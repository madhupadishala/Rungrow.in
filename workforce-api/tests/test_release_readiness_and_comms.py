import pytest
from types import SimpleNamespace
from app.communications import (
    IntegrationNotConfiguredError, TransactionalEmailGateway, WhatsAppCloudGateway,
)
from app.release_readiness import ReleaseReadinessService

class GatewayRepo:
    def __init__(self):
        self.audit=[]
        self.failed=[]
        self.completed=[]
        self.approval_consumptions=0
    async def append_audit(self,*args): self.audit.append(args)
    async def commit_boundary(self): return None
    async def consume_approval(self,*args,**kwargs):
        self.approval_consumptions+=1
        return {"id":"approval-once"}
    async def start_agent_run(self,run_id): return None
    async def fail_agent_run(self,run_id,error): self.failed.append((run_id,error))
    async def complete_agent_run(self,run_id,result): self.completed.append((run_id,result))

class FakeResponse:
    def raise_for_status(self): return None
    def json(self): return {"messages":[{"id":"provider-message-1"}]}

class FakeHttp:
    def __init__(self): self.calls=[]
    async def post(self,url,headers,json,timeout):
        self.calls.append({"url":url,"headers":headers,"json":json,"timeout":timeout})
        return FakeResponse()

@pytest.fixture
def execution_context():
    principal=SimpleNamespace(user_id="founder",tenant_id="tenant-a",
        permissions=frozenset({"communication.send"}),product_ids=frozenset({"photo-studio-growth-os"}))
    employee=SimpleNamespace(id="sales-ops",permissions=frozenset({"communication.send"}),
        department="Sales & Revenue",product_id="photo-studio-growth-os")
    run={"id":"run-1","tenant_id":"tenant-a","employee_id":"sales-ops",
         "product_id":"photo-studio-growth-os","is_sandbox":False}
    return principal,employee,run

@pytest.mark.asyncio
async def test_whatsapp_live_mode_fails_closed_without_credentials(monkeypatch,execution_context):
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN",raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID",raising=False)
    repo=GatewayRepo()
    principal,employee,run=execution_context
    gateway=WhatsAppCloudGateway(repo)
    with pytest.raises(IntegrationNotConfiguredError):
        await gateway.send(principal=principal,employee=employee,run=run,
            payload={"to":"919999999999","message":"Appointment details"},approval_id="approved-once")
    assert repo.approval_consumptions==0
    assert any(event[0]=="communication.integration_not_configured" for event in repo.audit)

@pytest.mark.asyncio
async def test_email_live_mode_fails_closed_without_credentials(monkeypatch,execution_context):
    monkeypatch.delenv("EMAIL_API_KEY",raising=False)
    monkeypatch.delenv("EMAIL_FROM_ADDRESS",raising=False)
    repo=GatewayRepo()
    principal,employee,run=execution_context
    with pytest.raises(IntegrationNotConfiguredError):
        await TransactionalEmailGateway(repo).send(principal=principal,employee=employee,run=run,
            payload={"to":"customer@example.com","subject":"Booking","message":"Your booking is confirmed"},
            approval_id="approved-once")
    assert repo.approval_consumptions==0
    assert any(event[0]=="communication.integration_not_configured" for event in repo.audit)

@pytest.mark.asyncio
async def test_sandbox_communication_is_hard_blocked(execution_context):
    repo=GatewayRepo()
    principal,employee,run=execution_context
    run["is_sandbox"]=True
    with pytest.raises(PermissionError,match="sandbox"):
        await WhatsAppCloudGateway(repo).send(principal=principal,employee=employee,run=run,
            payload={"to":"919999999999","message":"Never send"},approval_id="unused")
    assert not repo.completed
    assert any(event[0]=="communication.sandbox_blocked" for event in repo.audit)

@pytest.mark.asyncio
async def test_whatsapp_gateway_adds_ai_disclosure_and_uses_meta_api(monkeypatch,execution_context):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN","test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID","123456789")
    http=FakeHttp()
    repo=GatewayRepo()
    principal,employee,run=execution_context
    result=await WhatsAppCloudGateway(repo,http_client=http).send(
        principal=principal,employee=employee,run=run,
        payload={"to":"919999999999","message":"Your appointment is confirmed"},
        approval_id="approved-once")
    assert result["messages"][0]["id"]=="provider-message-1"
    sent=http.calls[0]["json"]["text"]["body"]
    assert "Your appointment is confirmed" in sent
    from app.commercial_intelligence import AI_IDENTITY_DISCLOSURE
    assert AI_IDENTITY_DISCLOSURE in sent
    assert repo.completed

@pytest.mark.asyncio
async def test_release_readiness_never_infers_go_from_empty_evidence(install_test_repository):
    result=await ReleaseReadinessService(install_test_repository).evaluate()
    assert result["overall_decision"]=="NO_GO"
    assert set(result["release_layers"])=={
        "RELEASE_1_WORKFORCE","RELEASE_2_PRODUCTS","RELEASE_3_COMMERCIAL"}
    assert all(g["status"]=="BLOCKED" for layer in result["release_layers"].values() for g in layer["gates"])

@pytest.mark.asyncio
async def test_readiness_api_exposes_all_three_layers(client):
    response=await client.get("/v1/release/readiness")
    assert response.status_code==200
    payload=response.json()
    assert payload["overall_decision"] in {"GO","NO_GO"}
    assert len(payload["release_layers"])==3

@pytest.mark.asyncio
async def test_rate_limiter_returns_429_and_retry_after(client,monkeypatch):
    from app import rate_limit
    monkeypatch.setitem(rate_limit.POLICIES,"/v1/commercial/prospects/discover",(1,60))
    first=await client.post("/v1/commercial/prospects/discover",json={})
    second=await client.post("/v1/commercial/prospects/discover",json={})
    assert first.status_code in {401,403,422}
    assert second.status_code==429
    assert int(second.headers["retry-after"])>=1


@pytest.mark.asyncio
async def test_health_live_is_independent_of_database(client):
    response=await client.get("/health/live")
    assert response.status_code==200
    assert response.json()["status"]=="alive"

@pytest.mark.asyncio
async def test_health_ready_requires_postgres_migrations_oidc_and_hmac(client,monkeypatch):
    from types import SimpleNamespace
    from app import health
    class Result:
        def __init__(self,rows=()): self.rows=rows
        def mappings(self): return self
        def all(self): return self.rows
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return None
        async def execute(self,query):
            if "SELECT version,filename,checksum_sha256" in str(query):
                return Result([{"version":v,"filename":name,"checksum_sha256":checksum}
                    for v,name,checksum in expected])
            return Result()
    expected=[(f"{n:03d}",f"{n:03d}_migration.sql",f"checksum-{n}") for n in range(1,9)]
    monkeypatch.setattr(health.settings,"database_url","postgresql+asyncpg://test")
    monkeypatch.setattr(health.settings,"app_env","production")
    monkeypatch.setattr(health,"migration_files",lambda:[
        (v,SimpleNamespace(name=name),checksum) for v,name,checksum in expected])
    monkeypatch.setattr(health,"session_factory",lambda:lambda:Session())
    monkeypatch.setenv("JWT_JWKS_URL","https://identity.example.com/.well-known/jwks.json")
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET","s"*40)
    monkeypatch.setenv("RUNGROW_ADAPTER_WEBHOOK_SECRET","h"*40)
    response=await client.get("/health/ready")
    assert response.status_code==200
    assert response.json()["checks"]["migrations"]["expected_versions"]==[f"{n:03d}" for n in range(1,9)]

@pytest.mark.asyncio
async def test_health_ready_returns_503_when_database_is_missing(client,monkeypatch):
    from app import health
    monkeypatch.setattr(health.settings,"database_url","")
    response=await client.get("/health/ready")
    assert response.status_code==503
    assert "postgresql" in response.json()["failed_checks"]


@pytest.mark.asyncio
async def test_in_memory_worker_heartbeat_counts_and_freshness(install_test_repository):
    repo=install_test_repository
    await repo.heartbeat("worker-test","STARTING")
    await repo.heartbeat("worker-test","BUSY")
    await repo.heartbeat("worker-test","IDLE",processed_increment=2,dead_letter_increment=1)
    rows=await repo.list_worker_heartbeats()
    assert len(rows)==1
    assert rows[0]["status"]=="IDLE"
    assert rows[0]["processed_count"]==2
    assert rows[0]["dead_letter_count"]==1
    assert rows[0]["is_fresh"] is True


@pytest.mark.asyncio
async def test_whatsapp_api_returns_503_and_audits_missing_live_provider(client,install_test_repository,monkeypatch):
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN",raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID",raising=False)
    repo=install_test_repository
    run=await repo.create_agent_run(product_id="photo-studio-growth-os",
        employee_id="pgm-photo-studio-growth-os",task="Send approved customer update",
        status="planned",summary="test",steps=[],correlation_id="release-test",is_sandbox=False)
    response=await client.post("/v1/communications/whatsapp/send",json={
        "run_id":str(run["id"]),"to":"919999999999","message":"Booking update",
        "approval_id":"not-consumed"
    })
    assert response.status_code==503
    assert any(event[0]=="communication.integration_not_configured" for event in repo.audit)

@pytest.mark.asyncio
async def test_release_signoff_requires_full_commit_sha(client):
    response=await client.post("/v1/release/signoffs",json={
        "release_layer":"RELEASE_1_WORKFORCE","gate_name":"ENGINEERING","status":"PASS",
        "evidence":{"ci_run":"https://github.com/example/actions/runs/1"},
        "candidate_sha":"abc123","environment":"staging"
    })
    assert response.status_code==422
