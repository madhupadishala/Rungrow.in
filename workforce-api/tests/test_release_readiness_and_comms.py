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
    expected=[(f"{n:03d}",f"{n:03d}_migration.sql",f"checksum-{n}") for n in range(1,10)]
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
    assert response.json()["checks"]["migrations"]["expected_versions"]==[f"{n:03d}" for n in range(1,10)]

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
    assert any(event.get("action")=="communication.integration_not_configured" for event in repo.audit)

@pytest.mark.asyncio
async def test_release_signoff_requires_full_commit_sha(client):
    response=await client.post("/v1/release/signoffs",json={
        "release_layer":"RELEASE_1_WORKFORCE","gate_name":"ENGINEERING","status":"PASS",
        "evidence":{"ci_run":"https://github.com/example/actions/runs/1"},
        "candidate_sha":"abc123","environment":"staging"
    })
    assert response.status_code==422


@pytest.mark.asyncio
async def test_health_ready_returns_503_on_checksum_drift_and_missing_identity_secrets(client,monkeypatch):
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
                return Result([{"version":"001","filename":"001_migration.sql","checksum_sha256":"wrong"}])
            return Result()
    expected=[(f"{n:03d}",f"{n:03d}_migration.sql",f"checksum-{n}") for n in range(1,10)]
    monkeypatch.setattr(health.settings,"database_url","postgresql+asyncpg://test")
    monkeypatch.setattr(health.settings,"app_env","production")
    monkeypatch.setattr(health,"migration_files",lambda:[
        (v,SimpleNamespace(name=name),checksum) for v,name,checksum in expected])
    monkeypatch.setattr(health,"session_factory",lambda:lambda:Session())
    monkeypatch.delenv("JWT_JWKS_URL",raising=False)
    monkeypatch.delenv("JWT_PUBLIC_KEY",raising=False)
    monkeypatch.delenv("STUDIO_GROWTH_OS_SERVICE_SECRET",raising=False)
    monkeypatch.delenv("RUNGROW_ADAPTER_WEBHOOK_SECRET",raising=False)
    response=await client.get("/health/ready")
    assert response.status_code==503
    failures=response.json()["failed_checks"]
    assert {"migrations","oidc","hmac"}.issubset(set(failures))


@pytest.mark.asyncio
async def test_postgres_sliding_window_is_shared_and_returns_retry(monkeypatch):
    import os
    from uuid import uuid4
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app import rate_limit
    database_url=os.getenv("RUNGROW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PostgreSQL CI database URL is unavailable")
    engine=create_async_engine(database_url)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    monkeypatch.setattr(rate_limit,"session_factory",lambda:factory)
    key="test-"+str(uuid4())
    try:
        first=await rate_limit._allow_postgres([key],1,60)
        second=await rate_limit._allow_postgres([key],1,60)
        assert first==(True,0)
        assert second[0] is False
        assert second[1]>=1
    finally:
        async with factory() as session:
            async with session.begin():
                await session.execute(text("DELETE FROM rate_limit_events WHERE scope_key=:key"),{"key":key})
        await engine.dispose()


class CohortReadinessRepo:
    def __init__(self,signoffs):
        self.signoffs=signoffs
    async def list_release_gate_signoffs(self):
        return self.signoffs
    async def list_worker_heartbeats(self,stale_after_seconds=90):
        return [{"worker_id":"worker-1","is_fresh":True}]
    async def verify_migration_chain(self):
        return {"ok":True,"missing":[],"drift":[]}


class CohortAdapterRegistry:
    product_ids=frozenset({
        "photo-studio-growth-os","clinics-counter","photo-studio-billing",
        "entrepreneur-instagram-tools","pharma-software-utilities",
        "artists-business-tools","growtoys",
    })
    def readiness(self):
        return [{"product_id":p,"readiness":"REGISTERED"} for p in sorted(self.product_ids)]


def _cohort_signoffs():
    sha="b"*40
    controls={
        "SECURITY":{
            "tenant_isolation":{"cross_tenant_denial":"verified"},
            "scope_bound_hmac_v1":{"timestamp_window":"300s","sorted_scopes":"verified"},
        },
        "AI_GOVERNANCE":{
            "single_use_human_approval":{"payload_binding":"verified","separation_of_duties":"verified"},
            "fail_closed_external_gateways":{"whatsapp_503":"verified","email_503":"verified"},
        },
        "RELIABILITY":{"backup_restore_test":{"restore_drill":"verified"}},
        "BUSINESS":{"product_owner_acceptance":{"founder":"accepted"}},
    }
    signoffs=[]
    for layer in ("RELEASE_1_WORKFORCE","RELEASE_2_PRODUCTS","RELEASE_3_COMMERCIAL"):
        for gate in ("ENGINEERING","SECURITY","INTEGRATION","AI_GOVERNANCE","RELIABILITY","BUSINESS","OPERATIONS"):
            evidence=dict(controls.get(gate,{}))
            if layer=="RELEASE_2_PRODUCTS" and gate=="INTEGRATION":
                evidence["live_backend_verification"]={
                    "photo-studio-growth-os":{"status":"PASS","evidence_ref":"studio-growth-os-ci"},
                    "clinics-counter":{"status":"PASS","evidence_ref":"clinics-counter-ci"},
                }
            if layer=="RELEASE_2_PRODUCTS" and gate=="BUSINESS":
                evidence["product_acceptance"]={
                    "photo-studio-growth-os":{"status":"PASS","evidence_ref":"photo-studio-acceptance"},
                    "clinics-counter":{"status":"PASS","evidence_ref":"clinics-acceptance"},
                }
            if layer=="RELEASE_3_COMMERCIAL" and gate=="INTEGRATION":
                evidence["live_communication_verification"]={
                    "whatsapp":{"status":"PASS","evidence_ref":"provider-receipt"},
                    "email":{"status":"PASS","evidence_ref":"provider-receipt"},
                }
            if layer=="RELEASE_3_COMMERCIAL" and gate=="BUSINESS":
                evidence["commercial_reconciliation"]={
                    name:{"status":"PASS","evidence_ref":name+"-reconciliation"}
                    for name in ("revenue","customers","billing","consent","support")
                }
            signoffs.append({"release_layer":layer,"gate_name":gate,"status":"PASS",
                "evidence":evidence,"candidate_sha":sha,"environment":"production",
                "occurred_at":"2026-11-20T00:00:00+00:00"})
    for pid in ("photo-studio-billing","entrepreneur-instagram-tools","pharma-software-utilities","artists-business-tools"):
        signoffs.insert(0,{"release_layer":"RELEASE_2_PRODUCTS","gate_name":"INTEGRATION",
            "status":"DEFERRED_WITH_ACCEPTANCE","candidate_sha":sha,"environment":"production",
            "occurred_at":"2026-11-20T00:01:00+00:00","evidence":{
                "cohort_id":"portfolio-expansion","product_id":pid,"repository_status":"NO_REPO_YET",
                "accepted_risk":"No independent product adapter is ready.","owner":"product-owner",
                "due_date":"2027-02-28","compensating_control":"Keep product writes and handoff disabled."}})
    signoffs.insert(0,{"release_layer":"RELEASE_2_PRODUCTS","gate_name":"INTEGRATION",
        "status":"DEFERRED_WITH_ACCEPTANCE","candidate_sha":sha,"environment":"production",
        "occurred_at":"2026-11-20T00:02:00+00:00","evidence":{
            "cohort_id":"portfolio-expansion","product_id":"growtoys","governance_mode":"DISCOVERY_ONLY",
            "accepted_risk":"Discovery only; no live handoff or outreach.","owner":"product-owner",
            "due_date":"2027-02-28","compensating_control":"HTTP 403 on live handoff and outreach."}})
    return signoffs


@pytest.mark.asyncio
async def test_cohort_readiness_accepts_product_deferrals_but_never_waives_security(monkeypatch):
    from app import release_readiness
    monkeypatch.setattr(release_readiness.settings,"database_url","postgresql+asyncpg://test")
    monkeypatch.setenv("JWT_JWKS_URL","https://identity.example.com/.well-known/jwks.json")
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET","s"*40)
    monkeypatch.setenv("RUNGROW_ADAPTER_WEBHOOK_SECRET","h"*40)
    repo=CohortReadinessRepo(_cohort_signoffs())
    service=ReleaseReadinessService(repo,adapter_registry=CohortAdapterRegistry())
    result=await service.evaluate(cohort_id="flagship-production")
    assert result["overall_decision"]=="GO"
    assert result["cohort_product_ids"]==["photo-studio-growth-os","clinics-counter"]
    assert result["deferred_products"]==[]

    # A portfolio expansion deferral cannot hide missing HMAC configuration.
    monkeypatch.delenv("RUNGROW_ADAPTER_WEBHOOK_SECRET")
    blocked=await service.evaluate(cohort_id="flagship-production")
    security=next(g for g in blocked["release_layers"]["RELEASE_2_PRODUCTS"]["gates"]
                  if g["signoff_gate"]=="SECURITY")
    assert security["status"]=="BLOCKED"
    assert any("HMAC" in item for item in security["missing_evidence"])


@pytest.mark.asyncio
async def test_gate_wide_security_or_reliability_deferral_is_a_hard_block(monkeypatch):
    from app import release_readiness
    monkeypatch.setattr(release_readiness.settings,"database_url","postgresql+asyncpg://test")
    monkeypatch.setenv("JWT_JWKS_URL","https://identity.example.com/.well-known/jwks.json")
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET","s"*40)
    monkeypatch.setenv("RUNGROW_ADAPTER_WEBHOOK_SECRET","h"*40)
    signoffs=_cohort_signoffs()
    for row in signoffs:
        if row["release_layer"]=="RELEASE_2_PRODUCTS" and row["gate_name"]=="SECURITY":
            row["status"]="DEFERRED_WITH_ACCEPTANCE"
            row["evidence"]={"accepted_risk":"Skip security"}
        if row["release_layer"]=="RELEASE_1_WORKFORCE" and row["gate_name"]=="RELIABILITY":
            row["status"]="DEFERRED_WITH_ACCEPTANCE"
            row["evidence"]={"accepted_risk":"Skip restore"}
    result=await ReleaseReadinessService(CohortReadinessRepo(signoffs),
        adapter_registry=CohortAdapterRegistry()).evaluate(cohort_id="flagship-production")
    security=next(g for g in result["release_layers"]["RELEASE_2_PRODUCTS"]["gates"]
                  if g["signoff_gate"]=="SECURITY")
    reliability=next(g for g in result["release_layers"]["RELEASE_1_WORKFORCE"]["gates"]
                     if g["signoff_gate"]=="RELIABILITY")
    assert security["status"]=="BLOCKED"
    assert reliability["status"]=="BLOCKED"


@pytest.mark.asyncio
async def test_production_bootstrap_is_idempotent_on_postgres(monkeypatch):
    import os
    from uuid import uuid4
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    from app.bootstrap_prod import bootstrap
    database_url=os.getenv("RUNGROW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PostgreSQL CI database URL is unavailable")
    tenant_id="bootstrap-test-"+str(uuid4())
    monkeypatch.setenv("DATABASE_URL",database_url)
    monkeypatch.setenv("RUNGROW_INTERNAL_TENANT_ID",tenant_id)
    monkeypatch.setenv("RUNGROW_RELEASE_CANDIDATE_SHA","c"*40)
    monkeypatch.setenv("APP_ENV","production")
    first=await bootstrap()
    second=await bootstrap()
    assert first==second
    assert first["tenant_slug"]=="rungrow"
    assert first["live_products"]==["photo-studio-growth-os","clinics-counter"]
    assert first["discovery_only"]==["growtoys"]
    assert first["product_bindings"]==3
    assert first["product_allocations"]==7
    assert first["deferred_signoffs"]==5
    engine=create_async_engine(database_url)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(text("SELECT set_config('app.current_tenant_id',:tenant,true)"),{"tenant":tenant_id})
                baseline=(await session.execute(text("""SELECT recognised_revenue_inr,raised_capital_inr,
                    revenue_target_inr,capital_target_inr,is_provisional FROM wf_mission_baselines
                    WHERE tenant_id=:tenant"""),{"tenant":tenant_id})).mappings().one()
                assert float(baseline["recognised_revenue_inr"])==0
                assert float(baseline["raised_capital_inr"])==0
                assert float(baseline["revenue_target_inr"])==50000000
                assert float(baseline["capital_target_inr"])==50000000
                assert baseline["is_provisional"] is True
                allocations=(await session.execute(text("""SELECT sum(revenue_target_inr) revenue,
                    sum(capital_target_inr) capital FROM wf_mission_product_allocations WHERE tenant_id=:tenant"""),
                    {"tenant":tenant_id})).mappings().one()
                assert float(allocations["revenue"])==50000000
                assert float(allocations["capital"])==50000000
    finally:
        await engine.dispose()
