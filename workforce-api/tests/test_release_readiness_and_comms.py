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


@pytest.mark.asyncio
async def test_release_readiness_deferrals_do_not_waive_flagship_security_or_reliability(monkeypatch):
    from app.release_readiness import GATES, LAYERS, ReleaseReadinessService
    sha = "a" * 40
    live = ("photo-studio-growth-os", "clinics-counter")
    all_products = set(live) | {"photo-studio-billing", "entrepreneur-instagram-tools",
        "pharma-software-utilities", "artists-business-tools", "growtoys"}
    class Registry:
        product_ids = frozenset(all_products)
        def readiness(self):
            return [{"product_id": p, "readiness": "DISCOVERY_ONLY" if p == "growtoys" else "REGISTERED_SECRET_REQUIRED"}
                for p in all_products]
    class Repo:
        def __init__(self):
            self.signoffs = []
            for layer in LAYERS:
                for _, gate in GATES:
                    evidence = {}
                    if layer == "RELEASE_1_WORKFORCE" and gate == "AI_GOVERNANCE":
                        evidence = {
                            "single_use_human_approval": {"payload_binding": "verified", "separation_of_duties": "verified"},
                            "fail_closed_external_gateways": {"whatsapp_503": "verified", "email_503": "verified"},
                        }
                    if layer == "RELEASE_1_WORKFORCE" and gate == "RELIABILITY":
                        evidence = {"backup_restore_test": {"status": "PASS", "evidence_ref": "restore-fixture"}}
                    if layer == "RELEASE_1_WORKFORCE" and gate == "BUSINESS":
                        evidence = {"product_owner_acceptance": {"status": "PASS", "evidence_ref": "owner-fixture"}}
                    if layer == "RELEASE_2_PRODUCTS" and gate == "SECURITY":
                        evidence = {"product_security_verification": {p: {
                            k: {"status": "PASS", "evidence_ref": p + "-" + k}
                            for k in ("hmac_v1", "tenant_isolation", "single_use_approval", "provider_fail_closed")}
                            for p in live}}
                    if layer == "RELEASE_2_PRODUCTS" and gate == "INTEGRATION":
                        evidence = {"live_backend_verification": {p: {"status": "PASS", "evidence_ref": p + "-live"} for p in live}}
                    if layer == "RELEASE_2_PRODUCTS" and gate == "BUSINESS":
                        evidence = {"product_acceptance": {p: {"status": "PASS", "evidence_ref": p + "-acceptance"} for p in live}}
                    if layer == "RELEASE_3_COMMERCIAL" and gate == "INTEGRATION":
                        evidence = {"live_communication_verification": {p: {"status": "PASS", "evidence_ref": p + "-receipt"} for p in ("whatsapp", "email")}}
                    if layer == "RELEASE_3_COMMERCIAL" and gate == "BUSINESS":
                        evidence = {"commercial_reconciliation": {p: {"status": "PASS", "evidence_ref": p + "-reconciled"} for p in ("revenue", "customers", "billing", "consent", "support")}}
                    self.signoffs.append({"release_layer": layer, "gate_name": gate, "status": "PASS",
                        "evidence": evidence, "candidate_sha": sha, "environment": "production",
                        "cohort_id": "flagship", "scope_key": "cohort"})
            for product_id in all_products - set(live):
                risk = "Accepted deferred integration risk for " + product_id
                self.signoffs.append({"release_layer": "RELEASE_2_PRODUCTS", "gate_name": "BUSINESS",
                    "status": "DEFERRED_WITH_ACCEPTANCE", "evidence": {"product_id": product_id,
                    "integration_status": "DISCOVERY_ONLY" if product_id == "growtoys" else "NO_REPO_YET",
                    "accepted_risk": risk, "risk_rationale": risk, "owner": "product-owner",
                    "due_date": "2027-02-28", "compensating_control": "Exclude from live cohort."},
                    "candidate_sha": sha, "environment": "production", "cohort_id": "portfolio",
                    "scope_key": product_id})
        async def list_release_gate_signoffs(self): return self.signoffs
        async def list_worker_heartbeats(self, stale_after_seconds=90): return [{"is_fresh": True}]
        async def verify_migration_chain(self): return {"ok": True, "missing": [], "drift": []}
    monkeypatch.setenv("JWT_JWKS_URL", "https://identity.example.test/jwks")
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET", "s" * 40)
    monkeypatch.setenv("RUNGROW_ADAPTER_WEBHOOK_SECRET", "h" * 40)
    monkeypatch.setenv("WORKFORCE_MODEL_PROVIDER", "openai-compatible")
    from app.config import settings
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://fixture")
    repo = Repo()
    service = ReleaseReadinessService(repo, adapter_registry=Registry())
    result = await service.evaluate(cohort="flagship")
    assert result["overall_decision"] == "GO"
    assert result["product_scope"]["required_live_products"] == list(live)
    assert set(result["product_scope"]["deferred_products"]) == all_products - set(live)

    # Product deferrals cannot waive the shared reliability gate.
    repo.signoffs.insert(0, {"release_layer": "RELEASE_1_WORKFORCE", "gate_name": "RELIABILITY",
        "status": "DEFERRED_WITH_ACCEPTANCE", "evidence": {"accepted_risk": "skip restore"},
        "candidate_sha": sha, "environment": "production", "cohort_id": "flagship", "scope_key": "cohort"})
    reliability_blocked = await service.evaluate(cohort="flagship")
    reliability = next(g for g in reliability_blocked["release_layers"]["RELEASE_1_WORKFORCE"]["gates"]
                       if g["signoff_gate"] == "RELIABILITY")
    assert reliability["status"] == "BLOCKED"
    repo.signoffs.pop(0)

    # Product deferrals cannot waive fail-closed external gateway evidence.
    ai_row = next(x for x in repo.signoffs if x["release_layer"] == "RELEASE_1_WORKFORCE" and x["gate_name"] == "AI_GOVERNANCE")
    ai_evidence = ai_row["evidence"]
    ai_row["evidence"] = {"single_use_human_approval": ai_evidence["single_use_human_approval"]}
    ai_blocked = await service.evaluate(cohort="flagship")
    ai_gate = next(g for g in ai_blocked["release_layers"]["RELEASE_1_WORKFORCE"]["gates"] if g["signoff_gate"] == "AI_GOVERNANCE")
    assert ai_gate["status"] == "BLOCKED"
    ai_row["evidence"] = ai_evidence

    repo.signoffs.insert(0, {"release_layer": "RELEASE_2_PRODUCTS", "gate_name": "SECURITY",
        "status": "DEFERRED_WITH_ACCEPTANCE", "evidence": {"accepted_risk": "x", "risk_rationale": "x",
        "owner": "owner", "due_date": "2027-02-28", "compensating_control": "none"},
        "candidate_sha": sha, "environment": "production", "cohort_id": "flagship", "scope_key": "cohort"})
    blocked = await service.evaluate(cohort="flagship")
    security = next(g for g in blocked["release_layers"]["RELEASE_2_PRODUCTS"]["gates"] if g["signoff_gate"] == "SECURITY")
    assert security["status"] == "BLOCKED"
    assert "Gate-wide deferral is prohibited" in " ".join(security["missing_evidence"])
    risk = "Attempted to defer an already-live flagship product"
    repo.signoffs.append({"release_layer": "RELEASE_2_PRODUCTS", "gate_name": "BUSINESS",
        "status": "DEFERRED_WITH_ACCEPTANCE", "evidence": {"product_id": "photo-studio-growth-os",
        "integration_status": "LIVE_BACKEND_WIRED", "accepted_risk": risk, "risk_rationale": risk,
        "owner": "owner", "due_date": "2027-02-28", "compensating_control": "none"},
        "candidate_sha": sha, "environment": "production", "cohort_id": "portfolio",
        "scope_key": "photo-studio-growth-os"})
    portfolio = await service.evaluate(cohort="portfolio")
    assert "photo-studio-growth-os" in portfolio["product_scope"]["required_live_products"]
    assert "photo-studio-growth-os" in portfolio["product_scope"]["invalid_deferred_products"]


@pytest.mark.asyncio
async def test_bootstrap_prod_is_idempotent_on_postgres(monkeypatch):
    import os
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.bootstrap_prod import bootstrap
    database_url = os.getenv("RUNGROW_TEST_DATABASE_URL")
    assert database_url, "PostgreSQL CI must provide RUNGROW_TEST_DATABASE_URL"
    monkeypatch.setenv("RUNGROW_INTERNAL_TENANT_ID", "rungrow")
    monkeypatch.setenv("RUNGROW_RELEASE_CANDIDATE_SHA", "b" * 40)
    monkeypatch.setenv("APP_ENV", "production")
    first = await bootstrap(database_url)
    second = await bootstrap(database_url)
    assert first == second
    assert first["product_bindings"] == 3
    assert first["financial_baselines"] == 7
    assert first["deferred_signoffs"] == 5
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','rungrow',true)"))
            tenant = (await conn.execute(text("SELECT slug,name FROM wf_tenants WHERE id='rungrow'"))).one()
            assert tenant == ("rungrow", "RunGrow")
            totals = (await conn.execute(text("SELECT count(*),sum(recognised_revenue_inr),sum(raised_capital_inr),sum(revenue_target_allocation_inr),sum(capital_target_allocation_inr) FROM wf_financial_baselines WHERE tenant_id='rungrow'"))).one()
            assert totals[0] == 7 and float(totals[1]) == 0 and float(totals[2]) == 0
            assert float(totals[3]) == 50_000_000 and float(totals[4]) == 50_000_000
            deferred = (await conn.execute(text("SELECT count(*) FROM release_gate_signoffs WHERE tenant_id='rungrow' AND cohort_id='portfolio' AND status='DEFERRED_WITH_ACCEPTANCE' AND scope_key <> 'cohort'"))).scalar_one()
            assert deferred == 5
    finally:
        await engine.dispose()
