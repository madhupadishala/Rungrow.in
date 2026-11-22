from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
import json
import re
import os
from pydantic import BaseModel, Field

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from .agent_runtime import AgentInput, AgentOrchestrator
from .config import settings, validate_production_environment
from .policy import Principal
from .registry import BY_ID, DEPARTMENTS, EMPLOYEES, public_registry
from .repositories import WorkforceRepositoryProtocol, PostgresWorkforceRepository, session_factory
from .schemas import ApprovalDecision, ApprovalExecutionValidation, AgentRunRequest, CommandRequest, MemoryWrite, TaskCreate
from .security import current_principal, require_permission
from .model_gateway import ModelGateway, MockModelProvider, OpenAICompatibleProvider, AnthropicProvider
from .orchestrator import DurableTaskOrchestrator
from .execution_boundary import ToolRegistry, ExecutionBoundaryGuard, ApprovalBoundaryError, SandboxViolation
from .schemas import OrchestratorDispatchRequest, ToolExecutionRequest
from .adapters.base import SignedWebhookVerifier
from .adapters.photo_studio_growth_os import PhotoStudioGrowthOSAdapter, register_photo_studio_growth_os_tools
from .digital_ceo import DigitalCEOService, is_synthetic_event
from .commercial_intelligence import (CommercialIntelligenceService, RobotsCompliantHttpFetcher, MockDirectoryFetcher, PRODUCT_PLAYBOOKS)
from .adapters.generic_product_adapter import GENERIC_PRODUCT_IDS, GenericAdapterContext, AdapterGovernanceError
from .adapters.registry import build_product_adapter_registry
from .health import router as health_router
from .rate_limit import RateLimitMiddleware
from .release_readiness import ReleaseReadinessService

app = FastAPI(title="RunGrow Workforce API", version="0.5.0")
app.add_middleware(RateLimitMiddleware)
app.include_router(health_router)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins,
    allow_credentials=True, allow_methods=["GET","POST"],
    allow_headers=["Authorization","Content-Type","Idempotency-Key","X-RunGrow-Signature","X-RunGrow-Timestamp","X-RunGrow-Tenant-ID","X-RunGrow-External-Tenant-Id","X-RunGrow-Product-ID","X-RunGrow-Org-ID","X-RunGrow-Scopes","X-Correlation-ID"])

_PORTFOLIO_PATH = Path(__file__).resolve().parents[2] / "data" / "workforce" / "product-portfolio.json"
with _PORTFOLIO_PATH.open("r", encoding="utf-8") as f:
    _portfolio = json.load(f)
PRODUCTS = {x["product_id"]: x for x in _portfolio.get("products", [])}
PRODUCT_ADAPTERS = build_product_adapter_registry()
PRODUCT_ROLE_IDS = {e.product_id:e.id for e in EMPLOYEES if e.product_id}
if set(PRODUCT_ROLE_IDS) != set(PRODUCTS):
    raise RuntimeError("Product portfolio and Product Growth Manager registries do not match")


@app.on_event("startup")
async def require_database_configuration():
    if settings.app_env == "production":
        validate_production_environment()
    if not settings.database_url and settings.app_env != "test":
        raise RuntimeError("DATABASE_URL is required; Workforce API refuses volatile in-memory production storage")


async def get_workforce_repo(p: Principal = Depends(current_principal)):
    if not settings.database_url:
        raise HTTPException(503, "DATABASE_URL is not configured")
    factory = session_factory()
    async with factory() as session:
        async with PostgresWorkforceRepository(session, p) as repository:
            yield repository


def validate_product_id(product_id: str | None):
    if product_id is not None and product_id not in PRODUCTS:
        raise HTTPException(404, "Unknown product scope")


def authorize_product_access(p: Principal, product_id: str | None):
    validate_product_id(product_id)
    if product_id is not None and "*" not in p.permissions and product_id not in p.product_ids:
        raise HTTPException(403, "Principal is not authorised for this product")


def enforce_product_scope(employee, product_id):
    validate_product_id(product_id)
    if employee.product_id and employee.product_id != product_id:
        raise HTTPException(403, "Product-scoped employee requires its exact assigned product_id")


def route(command_text: str):
    value=command_text.lower()
    mapping=[
      (("sales","pipeline","lead","prospect","deal"),("Sales & Revenue","sales-ops","crm.read")),
      (("campaign","seo","content","market"),("Growth & Marketing","market-intel","research.public")),
      (("customer","onboarding","renewal","adoption"),("Customer Success","cs-director","customer.read")),
      (("support","ticket","issue"),("Customer Support","support-lead","support.read")),
      (("product","roadmap","feature"),("Product & Design","pm","product.read")),
      (("code","engineering","deploy","bug"),("Engineering & IT","cto","engineering.read")),
      (("invoice","finance","billing","vendor"),("Finance & Administration","finance","finance.read")),
      (("hiring","candidate","employee"),("People & Culture","people-ops","people.read")),
      (("process","operations"),("Business Operations","bizops","operations.read")),
      (("contract","privacy","compliance","legal"),("Legal, Risk & Compliance","legal","compliance.read")),
    ]
    return next((result for words,result in mapping if any(w in value for w in words)),
                ("Executive Office","chief-staff","reports.read"))


def translate_repo_error(exc):
    if isinstance(exc, PermissionError): raise HTTPException(403,str(exc)) from exc
    if isinstance(exc, LookupError): raise HTTPException(404,str(exc)) from exc
    if isinstance(exc, ValueError): raise HTTPException(422,str(exc)) from exc
    raise exc


@app.get("/health")
async def health():
    return {"status":"ok","service":"rungrow-workforce-api","adapter":"postgresql"}


@app.get("/v1/workforce/registry")
async def registry(p: Principal = Depends(require_permission("workforce.read"))):
    return {"departments":list(DEPARTMENTS),"employees":public_registry(),"count":len(EMPLOYEES)}


@app.post("/v1/workforce/commands")
async def command(body: CommandRequest, p: Principal = Depends(current_principal),
                  repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    authorize_product_access(p,body.product_id)
    department,role_id,permission=route(body.command)
    if body.product_id: role_id=PRODUCT_ROLE_IDS[body.product_id]
    role=BY_ID[role_id]; enforce_product_scope(role,body.product_id)
    command_id=str(uuid4())
    if permission not in p.permissions and "*" not in p.permissions:
        return {"status":"denied","message":"Workspace role is not authorised.","command_id":command_id}
    if re.search(r"\b(send|launch|activate|charge|refund|delete|export|deploy|sign|purchase|discount|change price)\b",body.command,re.I):
        try:
            approval=await repo.create_approval(product_id=body.product_id,employee_id=role.id,
                action_type="command.execution",payload={"command":body.command,"role_id":role_id},
                expires_at=datetime.now(timezone.utc)+timedelta(minutes=15))
        except Exception as exc: translate_repo_error(exc)
        return {"status":"approval_required","command_id":command_id,"approval_id":approval["id"],
                "routed_department":department,"execution":"not_started"}
    return {"status":"accepted","message":"Accepted for planning; no external action performed.",
            "command_id":command_id,"routed_department":department,"routed_role_id":role_id}


@app.post("/v1/workforce/tasks")
async def create_task(body: TaskCreate, p: Principal = Depends(require_permission("work.assign")),
                      repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo),
                      idempotency_key: str | None = Header(default=None,alias="Idempotency-Key")):
    authorize_product_access(p,body.product_id)
    if body.assignee_role_id not in BY_ID: raise HTTPException(404,"Unknown employee role")
    employee=BY_ID[body.assignee_role_id]; enforce_product_scope(employee,body.product_id)
    try:
        return await repo.create_task(product_id=body.product_id,department=employee.department,
            assignee_role_id=body.assignee_role_id,title=body.title,description=body.description,
            priority=body.priority,idempotency_key=idempotency_key or str(uuid4()),is_sandbox=body.is_sandbox)
    except Exception as exc: translate_repo_error(exc)


@app.get("/v1/workforce/tasks")
async def tasks(product_id: str | None=None,p: Principal=Depends(require_permission("work.read")),
                repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,product_id)
    try: return await repo.list_tasks(product_id)
    except Exception as exc: translate_repo_error(exc)


@app.get("/v1/workforce/approvals")
async def approvals(product_id: str | None=None,p: Principal=Depends(require_permission("approval.read")),
                    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,product_id)
    try: return await repo.list_approvals(product_id)
    except Exception as exc: translate_repo_error(exc)


@app.post("/v1/workforce/approvals/{approval_id}/decision")
async def decide(approval_id: str,body: ApprovalDecision,p: Principal=Depends(require_permission("approval.decide")),
                 repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,body.product_id)
    try:
        item=await repo.decide_approval(approval_id,body.decision,body.reason,body.product_id)
        return {"id":approval_id,"status":item["status"],"execution":"not_started"}
    except Exception as exc: translate_repo_error(exc)


@app.post("/v1/workforce/approvals/{approval_id}/validate-execution")
async def validate_execution(approval_id: str,body: ApprovalExecutionValidation,
    p: Principal=Depends(require_permission("approval.execute")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,body.product_id)
    try:
        item=await repo.validate_approval(approval_id,product_id=body.product_id,
            employee_id=body.employee_id,action_type=body.action_type,payload=body.payload)
        await repo.append_audit("approval.execution_validated","approval",approval_id,
                                body.product_id,{"payload_hash":item["payload_hash"]})
        return {"approval_id":item["id"],"authorised":True,"execution":"not_started"}
    except Exception as exc: translate_repo_error(exc)


@app.post("/v1/workforce/memory")
async def memory(body: MemoryWrite,p: Principal=Depends(require_permission("memory.write")),
                 repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,body.product_id)
    employee=BY_ID.get(body.role_id)
    if employee is None: raise HTTPException(404,"Unknown role")
    enforce_product_scope(employee,body.product_id)
    try:
        item=await repo.write_memory(product_id=body.product_id,role_id=body.role_id,
            department=employee.department,content=body.content,source_ref=body.source_ref,
            retention_days=body.retention_days)
        return {"id":str(item["id"]),"scope":"tenant+department+role"}
    except Exception as exc: translate_repo_error(exc)


@app.get("/v1/workforce/memory")
async def read_memory(product_id: str | None=None,p: Principal=Depends(require_permission("memory.read")),
                      repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,product_id)
    try: return await repo.list_memory(product_id)
    except Exception as exc: translate_repo_error(exc)


@app.get("/v1/workforce/audit")
async def audit_events(product_id: str | None=None,p: Principal=Depends(require_permission("audit.read")),
                       repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,product_id)
    try: return (await repo.list_audit(product_id))[-500:]
    except Exception as exc: translate_repo_error(exc)


@app.get("/v1/workforce/agent-runs")
async def agent_runs(product_id: str | None=None,p: Principal=Depends(require_permission("work.read")),
                     repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,product_id)
    try: return await repo.list_agent_runs(product_id)
    except Exception as exc: translate_repo_error(exc)


@app.post("/v1/workforce/agent-runs")
async def run_employee(body: AgentRunRequest,p: Principal=Depends(require_permission("agent.run")),
                       repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,body.product_id)
    employee=BY_ID.get(body.role_id)
    if employee is None: raise HTTPException(404,"Unknown workforce employee")
    enforce_product_scope(employee,body.product_id)
    missing=set(employee.permissions)-p.permissions
    if "*" not in p.permissions and missing:
        raise HTTPException(403,"Invoker lacks employee tool scopes")
    plan=await AgentOrchestrator().run(employee,AgentInput(p.tenant_id,p.user_id,body.task,tuple(body.source_refs)))
    product_id=employee.product_id or body.product_id
    try:
        record=await repo.create_agent_run(product_id=product_id,employee_id=employee.id,task=body.task,
            status=plan.status,summary=plan.summary,steps=list(plan.steps),correlation_id=str(uuid4()),is_sandbox=body.is_sandbox)
        if plan.approval_required:
            approval=await repo.pause_run_for_approval(str(record["id"]),"agent.proposed_action",
                {"agent_run_id":str(record["id"]),"employee_id":employee.id,"product_id":product_id,
                 "task":body.task,"proposed_actions":list(plan.proposed_actions)})
            record["approval_id"]=approval["id"]
        return record
    except Exception as exc: translate_repo_error(exc)



class _WorkItemPlan(BaseModel):
    summary: str
    steps: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    action_type: str | None = None
    action_payload: dict = Field(default_factory=dict)


def get_model_gateway(repo):
    configured=os.getenv("WORKFORCE_MODEL_PROVIDER")
    provider_name=(configured or ("mock" if settings.app_env=="test" else "openai-compatible")).lower()
    model=os.getenv("WORKFORCE_PRIMARY_MODEL","gpt-4o-mini")
    def build(name):
        if name=="anthropic": return AnthropicProvider()
        if name in {"openai","openai-compatible","local"}: return OpenAICompatibleProvider()
        if name=="mock":
            return MockModelProvider([{"summary":"Deterministic offline task plan","steps":["Review task context","Prepare recommendation"],"requires_approval":False,"action_type":None,"action_payload":{}}])
        raise RuntimeError("Unsupported WORKFORCE_MODEL_PROVIDER: "+name)
    providers={provider_name:build(provider_name)}
    fallback=os.getenv("WORKFORCE_FALLBACK_PROVIDER")
    if fallback: providers[fallback]=build(fallback)
    return ModelGateway(providers,primary_provider=provider_name,primary_model=model,
        fallback_provider=fallback,fallback_model=os.getenv("WORKFORCE_FALLBACK_MODEL"),repository=repo)


def get_commercial_intelligence_service(repo: WorkforceRepositoryProtocol):
    if settings.app_env == "test":
        provider = MockModelProvider([{
            "name": "Campaign planning brief",
            "target_icp_segment": "Validated target segment",
            "channel": "RESEARCH_ONLY",
            "messaging_brief": "Research-led campaign plan. No outbound communication is sent.",
            "success_metric": "Qualified prospects",
        }])
        gateway = ModelGateway({"mock": provider}, primary_provider="mock",
            primary_model="commercial-intelligence-fixture", repository=repo)
        fetcher = MockDirectoryFetcher()
    else:
        gateway = get_model_gateway(repo)
        fetcher = RobotsCompliantHttpFetcher(
            allow_domains=set(filter(None, os.getenv("RUNGROW_RESEARCH_ALLOW_DOMAINS", "").split(","))),
            deny_domains=set(filter(None, os.getenv("RUNGROW_RESEARCH_DENY_DOMAINS", "").split(","))),
            requests_per_second=float(os.getenv("RUNGROW_RESEARCH_REQUESTS_PER_SECOND", "0.5")))
    return CommercialIntelligenceService(repo, fetcher, gateway)


def get_tool_registry(repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    registry = ToolRegistry()
    adapter = PhotoStudioGrowthOSAdapter()
    return register_photo_studio_growth_os_tools(registry, adapter, repo)

async def get_adapter_repo(
    tenant_id: str = Header(..., alias="X-RunGrow-External-Tenant-Id"),
    product_id: str = Header(..., alias="X-RunGrow-Product-ID"),
    org_id: str = Header(..., alias="X-RunGrow-Tenant-ID"),
):
    if product_id != "photo-studio-growth-os":
        raise HTTPException(403, "Adapter product binding mismatch")
    internal_tenant_id = os.getenv("RUNGROW_INTERNAL_TENANT_ID", "")
    if not internal_tenant_id:
        raise HTTPException(503, "RUNGROW_INTERNAL_TENANT_ID is required for adapter isolation")
    if org_id != internal_tenant_id or org_id == tenant_id:
        raise HTTPException(403, "External product tenant and internal Workforce tenant must be distinct")
    if not settings.database_url:
        raise HTTPException(503, "DATABASE_URL is required for durable adapter telemetry")
    service = Principal("studio-growth-os-webhook", internal_tenant_id, "service",
        frozenset({"product-telemetry:write"}), actor_type="service",
        product_ids=frozenset({product_id}))
    factory = session_factory()
    async with factory() as session:
        async with PostgresWorkforceRepository(session, service) as repository:
            yield repository

@app.post("/v1/adapters/photo-studio-growth-os/webhooks")
async def photo_studio_growth_os_webhook(
    request: Request,
    repo: WorkforceRepositoryProtocol = Depends(get_adapter_repo),
    signature: str | None = Header(default=None, alias="X-RunGrow-Signature"),
    timestamp: str | None = Header(default=None, alias="X-RunGrow-Timestamp"),
    tenant_header: str | None = Header(default=None, alias="X-RunGrow-External-Tenant-Id"),
    product_header: str | None = Header(default=None, alias="X-RunGrow-Product-ID"),
    org_header: str | None = Header(default=None, alias="X-RunGrow-Tenant-ID"),
    scopes_header: str | None = Header(default=None, alias="X-RunGrow-Scopes"),
    correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Invalid webhook JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "Webhook payload must be a JSON object")
    tenant_id = payload.get("tenant_id")
    product_id = payload.get("product_id")
    if product_id != "photo-studio-growth-os" or product_header != product_id:
        raise HTTPException(403, "Webhook product binding mismatch")
    if not tenant_id or tenant_header != tenant_id:
        raise HTTPException(403, "Webhook external tenant binding mismatch")
    internal_tenant_id = os.getenv("RUNGROW_INTERNAL_TENANT_ID", "")
    if not internal_tenant_id or payload.get("workforce_tenant_id") != internal_tenant_id or org_header != internal_tenant_id or internal_tenant_id == tenant_id:
        raise HTTPException(403, "Webhook internal Workforce tenant binding mismatch")
    if not correlation_id or len(correlation_id) > 128:
        raise HTTPException(400, "A valid X-Correlation-ID is required")
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key is required")
    scopes = (scopes_header or "").split(",")
    scopes = [scope.strip() for scope in scopes if scope.strip()]
    if not scopes or scopes != sorted(set(scopes)) or "photo-studio-growth-os:write" not in scopes:
        raise HTTPException(403, "Sorted photo-studio-growth-os:write scope is required")
    secret = os.getenv("STUDIO_GROWTH_OS_SERVICE_SECRET", "")
    if not secret:
        raise HTTPException(503, "STUDIO_GROWTH_OS_SERVICE_SECRET is not configured")
    try:
        SignedWebhookVerifier(secret).verify(raw, signature, timestamp,
            tenant_id=tenant_id, product_id=product_id,
            workforce_tenant_id=internal_tenant_id, scopes=frozenset(scopes))
        adapter = PhotoStudioGrowthOSAdapter(service_secret=secret)
        return await adapter.handle_webhook(body=raw, signature=signature, timestamp=timestamp,
            tenant_id=tenant_id, product_id=product_id, idempotency_key=idempotency_key,
            correlation_id=correlation_id, repository=repo, scopes=frozenset(scopes))
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc



async def get_generic_adapter_repo(
    product_id: str,
    external_tenant_id: str = Header(..., alias="X-RunGrow-External-Tenant-Id"),
    workforce_tenant_id: str = Header(..., alias="X-RunGrow-Tenant-ID"),
):
    if product_id not in GENERIC_PRODUCT_IDS:
        raise HTTPException(404, "Unknown generic adapter product")
    internal_tenant_id = os.getenv("RUNGROW_INTERNAL_TENANT_ID", "")
    if not internal_tenant_id:
        raise HTTPException(503, "RUNGROW_INTERNAL_TENANT_ID is required for adapter isolation")
    if workforce_tenant_id != internal_tenant_id or external_tenant_id == internal_tenant_id:
        raise HTTPException(403, "External product tenant and internal Workforce tenant must be distinct")
    if not settings.database_url:
        raise HTTPException(503, "DATABASE_URL is required for durable adapter telemetry")
    service = Principal("product-adapter-webhook", internal_tenant_id, "service",
        frozenset({"product-telemetry:write"}), actor_type="service",
        product_ids=frozenset({product_id}))
    factory = session_factory()
    async with factory() as session:
        async with PostgresWorkforceRepository(session, service) as repository:
            yield repository


@app.post("/v1/adapters/{product_id}/webhooks")
async def generic_product_webhook(
    product_id: str,
    request: Request,
    repo: WorkforceRepositoryProtocol = Depends(get_generic_adapter_repo),
    signature: str | None = Header(default=None, alias="X-RunGrow-Signature"),
    timestamp: str | None = Header(default=None, alias="X-RunGrow-Timestamp"),
    external_tenant_header: str | None = Header(default=None, alias="X-RunGrow-External-Tenant-Id"),
    workforce_tenant_header: str | None = Header(default=None, alias="X-RunGrow-Tenant-ID"),
    product_header: str | None = Header(default=None, alias="X-RunGrow-Product-ID"),
    scopes_header: str | None = Header(default=None, alias="X-RunGrow-Scopes"),
    correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if product_header != product_id:
        raise HTTPException(403, "Webhook product binding mismatch")
    if not external_tenant_header or not correlation_id or len(correlation_id) > 128 or not idempotency_key:
        raise HTTPException(400, "External tenant, correlation and idempotency headers are required")
    internal_tenant_id = os.getenv("RUNGROW_INTERNAL_TENANT_ID", "")
    if not internal_tenant_id or workforce_tenant_header != internal_tenant_id or external_tenant_header == internal_tenant_id:
        raise HTTPException(403, "Webhook external/internal tenant binding mismatch")
    scopes = (scopes_header or "").split(",")
    scopes = [scope.strip() for scope in scopes if scope.strip()]
    if not scopes or scopes != sorted(set(scopes)) or f"{product_id}:write" not in scopes:
        raise HTTPException(403, "Sorted product write scope is required")
    raw = await request.body()
    adapter = PRODUCT_ADAPTERS.get(product_id)
    try:
        return await adapter.handle_webhook(raw_body=raw, signature=signature, timestamp=timestamp,
            external_tenant_id=external_tenant_header, workforce_tenant_id=internal_tenant_id,
            scopes=frozenset(scopes), idempotency_key=idempotency_key,
            correlation_id=correlation_id, repository=repo)
    except AdapterGovernanceError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc



class GenericHandoffRequest(BaseModel):
    external_tenant_id: str = Field(min_length=2, max_length=200)
    run_id: str = Field(min_length=2, max_length=100)
    payload: dict = Field(default_factory=dict)
    approval_id: str | None = None
    is_sandbox: bool = False


@app.get("/v1/adapters/{product_id}/summary")
async def generic_adapter_summary(product_id: str,
    external_tenant_id: str = Header(..., alias="X-RunGrow-External-Tenant-ID"),
    p: Principal = Depends(require_permission("reports.read")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    if product_id not in GENERIC_PRODUCT_IDS:
        raise HTTPException(404, "Generic adapter product not found")
    authorize_product_access(p, product_id)
    context = GenericAdapterContext(tenant_id=external_tenant_id,
        workforce_tenant_id=p.tenant_id, product_id=product_id,
        correlation_id=str(uuid4()), scopes=p.permissions)
    try:
        return await PRODUCT_ADAPTERS.get(product_id).read(
            external_tenant_id=external_tenant_id, context=context, repository=repo)
    except AdapterGovernanceError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@app.post("/v1/adapters/{product_id}/handoff")
async def generic_adapter_handoff(product_id: str, body: GenericHandoffRequest,
    p: Principal = Depends(require_permission("agent.run")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    if product_id not in GENERIC_PRODUCT_IDS:
        raise HTTPException(404, "Generic adapter product not found")
    authorize_product_access(p, product_id)
    run = await repo.get_agent_run(body.run_id)
    if not run:
        raise HTTPException(404, "Agent run not found")
    if run.get("product_id") != product_id:
        raise HTTPException(403, "Agent run product binding mismatch")
    employee = BY_ID.get(run.get("employee_id"))
    if employee is None:
        raise HTTPException(403, "Agent run employee is unavailable")
    context = GenericAdapterContext(tenant_id=body.external_tenant_id,
        workforce_tenant_id=p.tenant_id, product_id=product_id,
        correlation_id=str(uuid4()), scopes=p.permissions, is_sandbox=body.is_sandbox)
    try:
        return await PRODUCT_ADAPTERS.get(product_id).handoff(payload=body.payload,
            context=context, principal=p, employee=employee, run=run,
            approval_id=body.approval_id, repository=repo)
    except AdapterGovernanceError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except (ApprovalBoundaryError, SandboxViolation) as exc:
        raise HTTPException(getattr(exc, "status_code", 403), str(exc)) from exc


@app.get("/v1/adapters/readiness")
async def adapter_readiness(p: Principal = Depends(require_permission("workforce.read"))):
    return {"adapters": PRODUCT_ADAPTERS.readiness(), "count": len(PRODUCT_ADAPTERS.product_ids)}


class ProspectDiscoveryRequest(BaseModel):
    product_id: str
    query: str = Field(min_length=2, max_length=300)
    city: str = Field(min_length=2, max_length=160)
    source_urls: list[str] = Field(default_factory=list, max_length=30)
    is_sandbox: bool = False


class CampaignPlanRequest(BaseModel):
    product_id: str
    target_icp_segment: str = Field(min_length=2, max_length=240)
    channel: str = Field(pattern="^(EMAIL|WHATSAPP|PHONE|SOCIAL|IN_PRODUCT|PARTNER|RESEARCH_ONLY)$")
    is_sandbox: bool = True


class CampaignActivationRequest(BaseModel):
    approval_id: str


@app.post("/v1/commercial/prospects/discover")
async def discover_prospects(body: ProspectDiscoveryRequest,
    p: Principal = Depends(require_permission("research.public")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    authorize_product_access(p, body.product_id)
    try:
        service = get_commercial_intelligence_service(repo)
        return await service.discover(tenant_id=p.tenant_id, product_id=body.product_id,
            query=body.query, city=body.city, source_urls=body.source_urls,
            is_sandbox=body.is_sandbox)
    except Exception as exc:
        translate_repo_error(exc)


@app.get("/v1/commercial/prospects")
async def list_commercial_prospects(product_id: str | None = None,
    p: Principal = Depends(require_permission("lead.enrich")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    authorize_product_access(p, product_id)
    try:
        return await repo.list_prospects(product_id=product_id)
    except Exception as exc:
        translate_repo_error(exc)


@app.post("/v1/commercial/campaigns")
async def create_commercial_campaign(body: CampaignPlanRequest,
    p: Principal = Depends(require_permission("campaign.draft")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    authorize_product_access(p, body.product_id)
    employee_id = PRODUCT_ROLE_IDS[body.product_id]
    try:
        service = get_commercial_intelligence_service(repo)
        brief = await service.generate_campaign_brief(tenant_id=p.tenant_id,
            product_id=body.product_id, segment=body.target_icp_segment,
            channel=body.channel, employee_id=employee_id, is_sandbox=body.is_sandbox)
        campaign = await service.create_campaign(product_id=body.product_id, brief=brief,
            is_sandbox=body.is_sandbox, created_by=p.user_id)
        return {"campaign": campaign, "execution": "draft_only"}
    except Exception as exc:
        translate_repo_error(exc)


@app.get("/v1/commercial/campaigns")
async def list_commercial_campaigns(product_id: str | None = None,
    p: Principal = Depends(require_permission("campaign.read")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    authorize_product_access(p, product_id)
    try:
        return await repo.list_campaigns(product_id=product_id)
    except Exception as exc:
        translate_repo_error(exc)


@app.post("/v1/commercial/campaigns/{campaign_id}/request-approval")
async def request_commercial_campaign_approval(campaign_id: str,
    p: Principal = Depends(require_permission("campaign.draft")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    try:
        campaign = await repo.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")
        authorize_product_access(p, campaign["product_id"])
        employee_id = PRODUCT_ROLE_IDS[campaign["product_id"]]
        service = get_commercial_intelligence_service(repo)
        approval = await service.request_campaign_approval(campaign=campaign, employee_id=employee_id)
        return {"campaign_id": campaign_id, "approval_id": approval["id"],
                "status": "AWAITING_APPROVAL", "execution": "not_started"}
    except HTTPException:
        raise
    except Exception as exc:
        translate_repo_error(exc)


@app.post("/v1/commercial/campaigns/{campaign_id}/activate")
async def activate_commercial_campaign(campaign_id: str, body: CampaignActivationRequest,
    p: Principal = Depends(require_permission("campaign.activate")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    try:
        campaign = await repo.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")
        authorize_product_access(p, campaign["product_id"])
        employee_id = PRODUCT_ROLE_IDS[campaign["product_id"]]
        employee = BY_ID[employee_id]
        run = await repo.create_agent_run(product_id=campaign["product_id"], employee_id=employee_id,
            task=f"Governed campaign activation {campaign_id}", status="planned",
            summary="Campaign activation through approval boundary", steps=[],
            correlation_id=str(uuid4()), is_sandbox=bool(campaign.get("is_sandbox", False)))
        service = get_commercial_intelligence_service(repo)
        return await service.activate_campaign(campaign_id=campaign_id,
            approval_id=body.approval_id, principal=p, employee=employee, run=run)
    except HTTPException:
        raise
    except Exception as exc:
        translate_repo_error(exc)


@app.post("/v1/workforce/orchestrator/tasks")
async def enqueue_orchestrator_task(body: TaskCreate,p: Principal=Depends(require_permission("work.assign")),
                                    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo),
                                    idempotency_key: str | None=Header(default=None,alias="Idempotency-Key")):
    authorize_product_access(p,body.product_id)
    employee=BY_ID.get(body.assignee_role_id)
    if employee is None: raise HTTPException(404,"Unknown employee role")
    enforce_product_scope(employee,body.product_id)
    try:
        return await repo.enqueue_task(product_id=body.product_id,department=employee.department,
            assignee_role_id=employee.id,title=body.title,description=body.description,priority=body.priority,
            is_sandbox=body.is_sandbox,idempotency_key=idempotency_key or str(uuid4()))
    except Exception as exc: translate_repo_error(exc)


@app.post("/v1/workforce/orchestrator/dispatch")
@app.post("/v1/workforce/orchestrator/step")
async def orchestrator_dispatch(body: OrchestratorDispatchRequest,
    p: Principal=Depends(require_permission("work.assign")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,body.product_id)
    worker_id=body.worker_id or f"api-worker:{p.user_id}:{uuid4()}"
    orchestrator=DurableTaskOrchestrator(repo,worker_id)
    task=None
    try:
        task=await orchestrator.claim(body.product_id)
        if not task: return {"status":"idle"}
        if body.product_id and task.get("product_id")!=body.product_id:
            await repo.fail_task(str(task["id"]),worker_id,"Product scope mismatch",0,task.get("max_attempts",5))
            raise HTTPException(403,"Claimed task is outside requested product scope")
        gateway=get_model_gateway(repo)
        planned,telemetry=await gateway.generate_structured(
            messages=[{"role":"user","content":("SANDBOX MODE: simulate only. " if task.get("is_sandbox") else "")+"Plan this workforce task without performing external side effects. Task: "+task["title"]+" Description: "+task.get("description","")}],
            output_model=_WorkItemPlan,tenant_id=p.tenant_id,product_id=task.get("product_id"),
            employee_id=task["assignee_role_id"],run_id=str(task["id"]),is_sandbox=bool(task.get("is_sandbox",False)))
        if planned.requires_approval:
            approval=await repo.pause_task_for_approval(str(task["id"]),planned.action_type or "task.sensitive_action",
                planned.action_payload or {"task_id":str(task["id"]),"summary":planned.summary})
            return {"task_id":str(task["id"]),"status":"AWAITING_APPROVAL","approval_id":approval["id"],
                "plan":planned.model_dump() if hasattr(planned,"model_dump") else planned.dict(),"telemetry":telemetry}
        result=planned.model_dump() if hasattr(planned,"model_dump") else planned.dict()
        await repo.finish_task(str(task["id"]),worker_id,result)
        return {"task_id":str(task["id"]),"status":"COMPLETED","result":result,"telemetry":telemetry}
    except HTTPException: raise
    except Exception as exc:
        if task and task.get("status")=="RUNNING":
            try: await repo.fail_task(str(task["id"]),worker_id,str(exc),2,task.get("max_attempts",5))
            except Exception: pass
        translate_repo_error(exc)


@app.post("/v1/workforce/orchestrator/tasks/{task_id}/resume")
async def resume_orchestrator_task(task_id: str, approval_id: str,
    p: Principal=Depends(require_permission("work.assign")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    try:
        task=next((x for x in await repo.list_tasks() if str(x["id"])==task_id),None)
        if not task: raise HTTPException(404,"Task not found")
        authorize_product_access(p,task.get("product_id"))
        resumed=await repo.resume_task(task_id,approval_id)
        return {"task_id":task_id,"status":resumed["status"],"execution":"not_started"}
    except HTTPException: raise
    except KeyError as exc: raise HTTPException(404,str(exc)) from exc
    except Exception as exc: translate_repo_error(exc)


@app.post("/v1/workforce/orchestrator/runs/{run_id}/resume")
async def resume_orchestrator_run(run_id: str, approval_id: str,
    p: Principal=Depends(require_permission("agent.run")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    run=await repo.get_agent_run(run_id)
    if not run: raise HTTPException(404,"Agent run not found")
    authorize_product_access(p,run.get("product_id"))
    approvals=await repo.list_approvals(run.get("product_id"))
    approval=next((x for x in approvals if x["id"]==approval_id),None)
    if not approval or str(approval.get("status","")).upper() not in {"APPROVED"} or approval.get("employee_id")!=run["employee_id"]:
        raise HTTPException(409,"Matching approved human decision required; execution still requires boundary validation")
    resumed=await repo.resume_agent_run(run_id,approval_id)
    return {"run_id":run_id,"status":resumed["execution_state"],"execution":"not_started"}


@app.post("/v1/workforce/tools/execute")
async def execute_registered_tool(body: ToolExecutionRequest,
    p: Principal=Depends(require_permission("agent.run")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo),
    registry: ToolRegistry=Depends(get_tool_registry)):
    try:
        run=await repo.get_agent_run(body.run_id)
        if not run: raise HTTPException(404,"Agent run not found")
        employee=BY_ID.get(run["employee_id"])
        if employee is None: raise HTTPException(403,"Employee role no longer exists")
        result=await ExecutionBoundaryGuard(repo,registry).execute(tool_name=body.tool_name,
            principal=p,employee=employee,run=run,payload=body.payload,approval_id=body.approval_id)
        return {"status":"completed","result":result}
    except (ApprovalBoundaryError,SandboxViolation) as exc:
        detail={"message":str(exc)}
        if getattr(exc,"approval_id",None): detail["approval_id"]=exc.approval_id
        raise HTTPException(getattr(exc,"status_code",403),detail) from exc
    except HTTPException: raise
    except KeyError as exc: raise HTTPException(404,str(exc)) from exc
    except Exception as exc: translate_repo_error(exc)


class CEODirectiveRequest(BaseModel):
    directive: str = Field(min_length=8, max_length=5000)
    product_ids: list[str] = Field(default_factory=list)
    is_sandbox: bool = True

class InvestmentCapitalEventRequest(BaseModel):
    event_type: str
    investor_ref: str = Field(min_length=1, max_length=240)
    amount_inr: Decimal = Field(default=Decimal('0'), ge=0)
    is_received: bool = False
    approval_id: str | None = None
    source_ref: str | None = Field(default=None, max_length=1000)
    metadata: dict = Field(default_factory=dict)
    occurred_at: datetime | None = None

class InvestmentApprovalRequest(BaseModel):
    event_type: str
    investor_ref: str = Field(min_length=1, max_length=240)
    amount_inr: Decimal = Field(gt=0)

def _ceo_service(repo):
    return DigitalCEOService(repo, BY_ID, PRODUCTS)

@app.get("/v1/ceo/briefing")
async def ceo_briefing(p: Principal=Depends(require_permission("executive.read")),
                       repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    try: return await _ceo_service(repo).briefing(generated_by=p.user_id)
    except Exception as exc: translate_repo_error(exc)

@app.post("/v1/ceo/directives")
async def ceo_directive(body: CEODirectiveRequest,
    p: Principal=Depends(require_permission("ceo.directive")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    if not body.directive.strip(): raise HTTPException(422,"Directive cannot be empty")
    products=body.product_ids or list(PRODUCTS)
    if len(set(products))!=len(products): raise HTTPException(422,"Duplicate product scope")
    for product_id in products:
        authorize_product_access(p,product_id)
    created=[]
    for product_id in products:
        role_id=PRODUCT_ROLE_IDS[product_id]
        employee=BY_ID[role_id]
        created.append(await repo.enqueue_task(product_id=product_id,department=employee.department,
            assignee_role_id=role_id,title=f"Founder directive: {body.directive[:120]}",
            description=body.directive,priority="high",is_sandbox=body.is_sandbox,
            idempotency_key=f"ceo-directive:{uuid4()}"))
    department, shared_role_id, _ = route(body.directive)
    shared_employee=BY_ID.get(shared_role_id)
    if shared_employee and shared_employee.product_id is None:
        created.append(await repo.enqueue_task(product_id=None,department=department,
            assignee_role_id=shared_role_id,title=f"Shared execution: {body.directive[:120]}",
            description=body.directive,priority="high",is_sandbox=body.is_sandbox,
            idempotency_key=f"ceo-directive-shared:{uuid4()}"))
    return {"status":"queued","task_count":len(created),"tasks":created,
            "is_sandbox":body.is_sandbox,"execution":"queued_not_executed"}

@app.get("/v1/dashboards/founder")
async def founder_dashboard(p: Principal=Depends(require_permission("executive.read")),
                            repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    return await _ceo_service(repo).briefing(generated_by=p.user_id)

@app.get("/v1/dashboards/ceo")
async def executive_dashboard(p: Principal=Depends(require_permission("executive.read")),
                              repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    tasks=await repo.list_tasks()
    runs=await repo.list_agent_runs()
    workload=[]
    for employee in EMPLOYEES:
        assigned=[t for t in tasks if t.get("assignee_role_id")==employee.id]
        assigned_runs=[r for r in runs if r.get("employee_id")==employee.id]
        active_tasks=sum(str(t.get("status","")).upper() in {"QUEUED","RUNNING","RETRY_SCHEDULED","AWAITING_APPROVAL"} for t in assigned)
        active_runs=sum(str(r.get("execution_state",r.get("status",""))).upper() in {"QUEUED","RUNNING","RETRY_SCHEDULED","AWAITING_APPROVAL"} for r in assigned_runs)
        occupied=any(str(t.get("status","")).upper() in {"RUNNING","AWAITING_APPROVAL"} for t in assigned) or any(str(r.get("execution_state",r.get("status",""))).upper() in {"RUNNING","AWAITING_APPROVAL"} for r in assigned_runs)
        workload.append({"employee_id":employee.id,"title":employee.title,"department":employee.department,
          "product_id":employee.product_id,"active_tasks":active_tasks+active_runs,
          "availability":"occupied" if occupied else "available",
          "completed_tasks":sum(str(t.get("status","")).upper()=="COMPLETED" for t in assigned)+sum(str(r.get("execution_state",r.get("status",""))).upper()=="COMPLETED" for r in assigned_runs)})
    return {"tasks":tasks,"agent_runs":runs,"employee_workload":workload,
      "sandbox_runs":sum(bool(x.get("is_sandbox")) for x in tasks+runs),
      "production_runs":sum(not bool(x.get("is_sandbox")) for x in tasks+runs),
      "escalations":[x for x in tasks+runs if str(x.get("status",x.get("execution_state",""))).upper() in {"DEAD_LETTER","FAILED","AWAITING_APPROVAL"} or str(x.get("execution_state","")).upper()=="DEAD_LETTER"]}

@app.get("/v1/dashboards/pgm/{product_id}")
async def pgm_dashboard(product_id: str,p: Principal=Depends(require_permission("reports.read")),
                        repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,product_id)
    tasks=await repo.list_tasks(product_id)
    telemetry=await repo.list_product_telemetry()
    approvals=await repo.list_approvals(product_id)
    product_events=[e for e in telemetry if e.get("product_id")==product_id and not is_synthetic_event(e)]
    revenue=sum(float(e.get("recognised_revenue_inr") or 0) for e in product_events if e.get("event_type")=="invoice_recognised")
    counts={kind:sum(e.get("event_type")==kind for e in product_events) for kind in
      ("studio_tenant_onboarded","trial_started","lead_converted","booking_confirmed","churn_risk_flagged")}
    counts.update({"prospects":None,"leads":None,"qualified_opportunities":None,
      "bookings":counts["booking_confirmed"],"conversions":counts["lead_converted"],
      "churn_risk_signals":counts["churn_risk_flagged"],"campaign_execution":None,
      "funnel_data_note":"Prospect, lead, qualified-opportunity and campaign counts are not yet instrumented; null is intentional."})
    return {"product_id":product_id,"funnel":counts,"recognised_revenue_inr":revenue,
      "data_source":"product_telemetry_events","is_synthetic":False,"tasks":tasks,
      "pending_approvals":[a for a in approvals if str(a.get("status","")).upper()=="PENDING"]}

@app.post("/v1/finance/investment-events")
async def record_investment_event(body: InvestmentCapitalEventRequest,
    p: Principal=Depends(require_permission("investment.record")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    try:
        return await repo.record_investment_capital_event(event_type=body.event_type,
            investor_ref=body.investor_ref,amount_inr=body.amount_inr,
            is_received=body.is_received,approval_id=body.approval_id,
            source_ref=body.source_ref,metadata=body.metadata,occurred_at=body.occurred_at)
    except Exception as exc:
        translate_repo_error(exc)

@app.post("/v1/finance/investment-approvals")
async def request_investment_approval(body: InvestmentApprovalRequest,
    p: Principal=Depends(require_permission("investment.record")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    if body.event_type not in {"term_sheet_signed","commitment_approved","capital_received"}:
        raise HTTPException(422,"Investment approval is only required for signed, committed or received capital events")
    try:
        approval=await repo.create_approval(product_id=None,employee_id="finance",
            action_type="investment.commit",
            payload={"event_type":body.event_type,"investor_ref":body.investor_ref,"amount_inr":body.amount_inr},
            expires_at=datetime.now(timezone.utc)+timedelta(hours=24))
        return {"approval_id":str(approval["id"]),"status":approval["status"],
                "event_type":body.event_type,"execution":"not_started"}
    except Exception as exc:
        translate_repo_error(exc)


class ReleaseSignoffRequest(BaseModel):
    release_layer: str
    gate_name: str
    status: str
    evidence: dict
    candidate_sha: str | None = None
    environment: str = "production"
    cohort_id: str = "portfolio"
    scope_key: str = "cohort"


@app.get("/v1/release/readiness")
async def release_readiness(
    cohort: str = "portfolio",
    p: Principal = Depends(require_permission("workforce.read")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo),
):
    try:
        return await ReleaseReadinessService(repo, adapter_registry=PRODUCT_ADAPTERS).evaluate(cohort=cohort)
    except Exception as exc:
        translate_repo_error(exc)


@app.post("/v1/release/signoffs")
async def record_release_signoff(
    body: ReleaseSignoffRequest,
    p: Principal = Depends(require_permission("release.approve")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo),
):
    verifier_type = "AUTOMATED" if getattr(p, "actor_type", "human") == "service" else "HUMAN"
    try:
        return await repo.record_release_gate_signoff(
            release_layer=body.release_layer, gate_name=body.gate_name,
            status=body.status, evidence=body.evidence, verifier_type=verifier_type,
            verifier_id=p.user_id, candidate_sha=body.candidate_sha,
            environment=body.environment, cohort_id=body.cohort_id, scope_key=body.scope_key,
        )
    except Exception as exc:
        translate_repo_error(exc)


class CommunicationSendRequest(BaseModel):
    run_id: str = Field(min_length=2, max_length=100)
    to: str = Field(min_length=3, max_length=320)
    message: str = Field(min_length=1, max_length=10000)
    approval_id: str | None = None

class EmailSendRequest(CommunicationSendRequest):
    subject: str = Field(min_length=1, max_length=240)


async def _execute_governed_communication(channel: str, body, principal: Principal, repo):
    from .communications import IntegrationNotConfiguredError, TransactionalEmailGateway, WhatsAppCloudGateway
    run=await repo.get_agent_run(body.run_id)
    if not run:
        raise HTTPException(404,"Agent run not found in this tenant")
    authorize_product_access(principal,run.get("product_id"))
    employee=BY_ID.get(run.get("employee_id"))
    if employee is None:
        raise HTTPException(403,"Agent role is unavailable")
    payload={"to":body.to,"message":body.message}
    if channel=="email":
        payload["subject"]=body.subject
        gateway=TransactionalEmailGateway(repo)
    else:
        gateway=WhatsAppCloudGateway(repo)
    try:
        return await gateway.send(principal=principal,employee=employee,run=run,
            payload=payload,approval_id=body.approval_id)
    except IntegrationNotConfiguredError as exc:
        raise HTTPException(503,str(exc)) from exc
    except ApprovalBoundaryError as exc:
        raise HTTPException(exc.status_code,{"detail":str(exc),"approval_id":exc.approval_id}) from exc
    except SandboxViolation as exc:
        raise HTTPException(403,str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403,str(exc)) from exc


@app.post("/v1/communications/whatsapp/send")
async def send_governed_whatsapp(body: CommunicationSendRequest,
    p: Principal=Depends(require_permission("communication.send")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    return await _execute_governed_communication("whatsapp",body,p,repo)


@app.post("/v1/communications/email/send")
async def send_governed_email(body: EmailSendRequest,
    p: Principal=Depends(require_permission("communication.send")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    return await _execute_governed_communication("email",body,p,repo)


# Workforce 2.1: hierarchical executive directives and department work orders.
from .orchestration import WorkforceOrchestrator

class FounderDirectiveRequest(BaseModel):
    command: str = Field(min_length=8, max_length=8000)
    product_id: str = Field(min_length=2, max_length=120)

class FounderDirectiveApproval(BaseModel):
    approval_id: str = Field(min_length=20, max_length=100)

@app.post("/v1/directives")
async def submit_founder_directive(body: FounderDirectiveRequest,
    p: Principal = Depends(require_permission("work.assign")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    authorize_product_access(p, body.product_id)
    try:
        return await WorkforceOrchestrator(repo, p).submit_directive(body.command, body.product_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@app.get("/v1/directives/{directive_id}")
async def get_founder_directive(directive_id: str,
    p: Principal = Depends(require_permission("work.read")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    result = await WorkforceOrchestrator(repo, p).get_directive(directive_id)
    if not result:
        raise HTTPException(404, "Directive not found")
    authorize_product_access(p, result.get("product_id"))
    return result

@app.post("/v1/directives/{directive_id}/approve")
async def approve_founder_directive(directive_id: str, body: FounderDirectiveApproval,
    p: Principal = Depends(require_permission("approval.decide")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    service = WorkforceOrchestrator(repo, p)
    directive = await service.get_directive(directive_id)
    if not directive:
        raise HTTPException(404, "Directive not found")
    authorize_product_access(p, directive.get("product_id"))
    if directive["status"] != "AWAITING_APPROVAL" or str(directive.get("approval_id")) != body.approval_id:
        raise HTTPException(409, "Directive is not awaiting this approval")
    try:
        await repo.consume_approval(body.approval_id, product_id=directive.get("product_id"),
            employee_id="digital-ceo", action_type="directive.external_execution",
            payload=directive["consolidated_proposal"], run_id=directive_id)
        return await service.approve_external(directive_id, body.approval_id)
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc

class SingleUseApprovalRequest(BaseModel):
    reason: str = Field(default="Founder approved governed dispatch", max_length=1000)

@app.post("/v1/approvals/{approval_id}/approve")
async def approve_single_use_outbox_action(approval_id: str,
    body: SingleUseApprovalRequest,
    p: Principal = Depends(require_permission("approval.decide")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo)):
    from sqlalchemy import text
    import httpx
    approval_rows=await repo.list_approvals()
    approval=next((x for x in approval_rows if str(x["id"])==approval_id),None)
    if not approval:
        raise HTTPException(404,"Approval not found")
    authorize_product_access(p,approval.get("product_id"))
    if approval.get("status")!="PENDING":
        raise HTTPException(409,"Approval is not pending or was already consumed")
    outbox=await repo._one("""SELECT * FROM wf_outbox_events
      WHERE approval_id=:approval AND tenant_id=:tenant FOR UPDATE""",
      {"approval":approval_id,"tenant":p.tenant_id})
    if not outbox:
        raise HTTPException(404,"Governed outbox event not found")
    if outbox["payload_hash"]!=approval["payload_hash"]:
        raise HTTPException(409,"Outbox payload hash does not match approval token")
    approved=await repo.decide_approval(approval_id,"approved",body.reason,approval.get("product_id"))
    event=outbox["payload"] if isinstance(outbox["payload"],dict) else json.loads(outbox["payload"])
    dispatch=event.get("dispatch_payload") or {}
    await repo.session.execute(text("""UPDATE wf_outbox_events SET status='DISPATCHING',
      attempt_count=attempt_count+1,updated_at=now() WHERE id=:id AND tenant_id=:tenant
      AND status='QUEUED_FOR_APPROVAL'"""),{"id":outbox["id"],"tenant":p.tenant_id})
    try:
        if approval["action_type"] in {"whatsapp.send","email.send"}:
            from .communications import IntegrationNotConfiguredError, TransactionalEmailGateway, WhatsAppCloudGateway
            employee=BY_ID.get(approval["employee_id"])
            if employee is None:
                raise HTTPException(403,"Approving employee role is unavailable")
            if not dispatch.get("to") or not dispatch.get("message"):
                await repo.session.execute(text("""UPDATE wf_outbox_events SET status='BLOCKED_INVALID_RECIPIENT',
                  last_error='A verified recipient address and message are required',updated_at=now()
                  WHERE id=:id AND tenant_id=:tenant"""),{"id":outbox["id"],"tenant":p.tenant_id})
                await repo.append_audit("outbox.dispatch_blocked_invalid_recipient","outbox_event",
                    str(outbox["id"]),approval["product_id"],{"approval_id":approval_id})
                raise HTTPException(422,"A verified recipient address is required before dispatch")
            run=await repo.create_agent_run(product_id=approval["product_id"],employee_id=employee.id,
                task="Founder-approved outbound communication",status="planned",
                summary="Governed dispatch from single-use approval",steps=[],
                correlation_id=str(outbox["id"]),is_sandbox=False)
            gateway=WhatsAppCloudGateway(repo) if approval["action_type"]=="whatsapp.send" else TransactionalEmailGateway(repo)
            try:
                result=await gateway.send(principal=p,employee=employee,run=run,
                    payload=dispatch,approval_id=approval_id)
            except IntegrationNotConfiguredError as exc:
                await repo.session.execute(text("""UPDATE wf_outbox_events SET status='BLOCKED_UNCONFIGURED',
                  last_error=:error,updated_at=now() WHERE id=:id AND tenant_id=:tenant"""),
                  {"error":str(exc)[:1000],"id":outbox["id"],"tenant":p.tenant_id})
                await repo.append_audit("outbox.dispatch_blocked_unconfigured","outbox_event",
                    str(outbox["id"]),approval["product_id"],{"approval_id":approval_id,"channel":event.get("channel")})
                raise HTTPException(503,str(exc)) from exc
            await repo.session.execute(text("""UPDATE wf_outbox_events SET status='SENT',
              provider_response=CAST(:response AS jsonb),dispatched_at=now(),updated_at=now()
              WHERE id=:id AND tenant_id=:tenant"""),
              {"response":json.dumps(result,default=str),"id":outbox["id"],"tenant":p.tenant_id})
            await repo.append_audit("outbox.communication_sent","outbox_event",str(outbox["id"]),
                approval["product_id"],{"approval_id":approval_id,"channel":event.get("channel")})
            return {"approval_id":approval_id,"status":"SENT","provider_result":result}
        if approval["action_type"]=="product.onboarding.dispatch":
            payload=dispatch
            adapter_payload=payload.get("adapter_payload") or {}
            headers=payload.get("signature_headers") or {}
            employee=BY_ID.get(approval["employee_id"])
            if employee is None:
                raise HTTPException(403,"Onboarding employee role is unavailable")
            run=await repo.create_agent_run(product_id=approval["product_id"],employee_id=employee.id,
                task="Founder-approved product onboarding adapter dispatch",status="planned",
                summary="Signed v1 adapter dispatch attempt",steps=[],
                correlation_id=str(outbox["id"]),is_sandbox=False)
            await repo.consume_approval(approval_id,product_id=approval["product_id"],
                employee_id=employee.id,action_type=approval["action_type"],payload=payload,run_id=run["id"])
            product=adapter_payload.get("product_id","")
            url=os.getenv("RUNGROW_PRODUCT_ADAPTER_DISPATCH_URL") or os.getenv(
                "RUNGROW_ADAPTER_DISPATCH_URL_"+product.upper().replace("-","_"),"")
            if not url:
                await repo.session.execute(text("""UPDATE wf_outbox_events SET status='BLOCKED_UNCONFIGURED',
                  last_error='Product adapter dispatch URL is not configured',updated_at=now()
                  WHERE id=:id AND tenant_id=:tenant"""),{"id":outbox["id"],"tenant":p.tenant_id})
                await repo.append_audit("outbox.adapter_dispatch_blocked_unconfigured","outbox_event",
                    str(outbox["id"]),approval["product_id"],{"approval_id":approval_id,
                    "signature_present":bool(headers.get("X-RunGrow-Signature"))})
                raise HTTPException(503,"Product adapter dispatch URL is not configured")
            raw=json.dumps(adapter_payload,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
            request_headers={**headers,"Content-Type":"application/json",
                "X-RunGrow-External-Tenant-Id":str(adapter_payload.get("external_tenant_id","")),
                "X-RunGrow-Tenant-ID":str(adapter_payload.get("workforce_tenant_id","")),
                "X-RunGrow-Product-ID":product,"X-RunGrow-Scopes":"tenant.provision",
                "X-Correlation-ID":str(adapter_payload.get("correlation_id",""))}
            async with httpx.AsyncClient(timeout=15.0) as client:
                response=await client.post(url,content=raw,headers=request_headers)
            response.raise_for_status()
            response_data={"http_status":response.status_code,"response_hash":__import__("hashlib").sha256(response.content).hexdigest(),
                "delivery":"adapter_acknowledged_not_verified"}
            await repo.session.execute(text("""UPDATE wf_outbox_events SET status='DISPATCH_ACCEPTED',
              provider_response=CAST(:response AS jsonb),dispatched_at=now(),updated_at=now()
              WHERE id=:id AND tenant_id=:tenant"""),
              {"response":json.dumps(response_data),"id":outbox["id"],"tenant":p.tenant_id})
            await repo.append_audit("outbox.adapter_dispatch_attempted","outbox_event",str(outbox["id"]),
                approval["product_id"],{"approval_id":approval_id,"signature_present":True,
                "http_status":response.status_code,"delivery":"not_claimed"})
            return {"approval_id":approval_id,"status":"DISPATCH_ACCEPTED","delivery":"not_claimed"}
        if approval["action_type"]=="social.publish":
            employee=BY_ID.get("social-publisher")
            if employee is None: raise HTTPException(403,"Social publisher role is unavailable")
            run=await repo.create_agent_run(product_id=approval["product_id"],employee_id=employee.id,
                task="Founder-approved social publishing dispatch",status="planned",
                summary="Single-use approval-gated social post dispatch",steps=[],
                correlation_id=str(outbox["id"]),is_sandbox=False)
            await repo.consume_approval(approval_id,product_id=approval["product_id"],
                employee_id=employee.id,action_type="social.publish",payload=dispatch,run_id=run["id"])
            endpoint=os.getenv("RUNGROW_SOCIAL_PUBLISH_DISPATCH_URL","").strip()
            secret=os.getenv("RUNGROW_SOCIAL_PUBLISH_SECRET","")
            if not endpoint or not secret:
                await repo.session.execute(text("""UPDATE wf_outbox_events SET status='BLOCKED_UNCONFIGURED',
                  last_error='Social publishing gateway URL or signing secret is not configured',updated_at=now()
                  WHERE id=:id AND tenant_id=:tenant"""),{"id":outbox["id"],"tenant":p.tenant_id})
                await repo.append_audit("social.publish_blocked_unconfigured","outbox_event",
                    str(outbox["id"]),approval["product_id"],{"approval_id":approval_id,"channel":event.get("channel")})
                raise HTTPException(503,"Social publishing gateway is not configured; approval consumed and dispatch blocked")
            raw=json.dumps(dispatch,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
            import hmac, hashlib
            signature=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
            async with httpx.AsyncClient(timeout=15.0,follow_redirects=False) as client:
                response=await client.post(endpoint,content=raw,headers={
                    "Content-Type":"application/json","X-RunGrow-Signature":signature,
                    "Idempotency-Key":approval_id,"X-RunGrow-Approval-ID":approval_id})
            response.raise_for_status()
            proof={"http_status":response.status_code,
                "response_sha256":hashlib.sha256(response.content).hexdigest(),
                "delivery":"gateway_acknowledged_not_verified"}
            await repo.session.execute(text("""UPDATE wf_outbox_events SET status='DISPATCH_ACCEPTED',
              provider_response=CAST(:proof AS jsonb),dispatched_at=now(),updated_at=now()
              WHERE id=:id AND tenant_id=:tenant"""),
              {"proof":json.dumps(proof),"id":outbox["id"],"tenant":p.tenant_id})
            await repo.append_audit("social.publish_dispatch_accepted","outbox_event",str(outbox["id"]),
                approval["product_id"],{"approval_id":approval_id,"channel":event.get("channel"),
                "delivery":"not_claimed"})
            return {"approval_id":approval_id,"status":"DISPATCH_ACCEPTED","delivery":"gateway_acknowledged_not_verified"}
        raise HTTPException(403,"Approval action is not an outbox dispatch action")
    except HTTPException:
        raise
    except Exception as exc:
        await repo.session.execute(text("""UPDATE wf_outbox_events SET status='FAILED',
          last_error=:error,updated_at=now() WHERE id=:id AND tenant_id=:tenant"""),
          {"error":str(exc)[:1000],"id":outbox["id"],"tenant":p.tenant_id})
        await repo.append_audit("outbox.dispatch_failed","outbox_event",str(outbox["id"]),
            approval["product_id"],{"approval_id":approval_id,"error_type":type(exc).__name__})
        raise HTTPException(502,"Governed dispatch failed; inspect the outbox audit event") from exc


# Workforce 2.3 — authenticated realtime Founder voice interface.
from .voice_ceo import (
    AI_IDENTITY_DISCLOSURE, LiveKitNotConfigured, VoiceCEOSessionManager,
    VoiceSessionForbidden,
)

class VoiceSessionCreateRequest(BaseModel):
    client_mode: str = Field(default="livekit", pattern="^(browser|livekit)$")
    product_id: str | None = Field(default=None, max_length=120)

class VoiceTurnRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=8000)
    source: str = Field(default="web_speech", pattern="^(web_speech|livekit|text)$")
    interrupted_at_ms: int | None = Field(default=None, ge=0)

_voice_sessions = VoiceCEOSessionManager()

def _spoken_executive_briefing(snapshot: dict) -> str:
    revenue = snapshot.get("operating_revenue") or {}
    capital = snapshot.get("investment_capital") or {}
    tasks = snapshot.get("tasks") or []
    completed = sum(int(row.get("completed") or 0) for row in tasks)
    active = sum(int(row.get("active") or 0) for row in tasks)
    pending = snapshot.get("pending_approvals") or []
    department_rows = [row for row in tasks if row.get("department")]
    department_summary = "; ".join(
        f"{row['department']}: {row.get('completed', 0)} completed, {row.get('active', 0)} active"
        for row in department_rows[:5]
    ) or "Department-level task evidence is not yet available."
    return (
        f"{AI_IDENTITY_DISCLOSURE} Executive briefing. "
        f"Operating revenue is ₹{float(revenue.get('actual_inr') or 0):,.0f} against the ₹5 crore baseline. "
        f"Investment capital received is ₹{float(capital.get('received_inr') or 0):,.0f} against a separate ₹5 crore baseline. "
        f"These are separate ledgers and neither figure is inferred. "
        f"There are {len(pending)} pending approvals, {completed} completed tasks and {active} active tasks. "
        f"{department_summary} "
        + ("The highest priority is to review pending approvals before expiry." if pending
           else "There are no pending approvals in the current repository snapshot.")
    )

@app.post("/v1/voice/sessions")
async def create_voice_session(
    body: VoiceSessionCreateRequest,
    p: Principal = Depends(require_permission("voice.use")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo),
):
    try:
        return await _voice_sessions.create_session(
            repo, p, client_mode=body.client_mode, product_id=body.product_id
        )
    except LiveKitNotConfigured as exc:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={
            "detail": str(exc), "session_id": exc.session_id, "live_room": "unavailable"
        })
    except VoiceSessionForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@app.post("/v1/voice/sessions/{session_id}/turn")
async def process_voice_turn(
    session_id: str,
    body: VoiceTurnRequest,
    p: Principal = Depends(require_permission("voice.use")),
    repo: WorkforceRepositoryProtocol = Depends(get_workforce_repo),
):
    try:
        turn = await _voice_sessions.append_turn(
            repo, p, session_id, body.transcript,
            interrupted_at_ms=body.interrupted_at_ms, source=body.source
        )
    except VoiceSessionForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    transcript = turn["transcript"].strip()
    lowered = transcript.lower()
    response = ""
    action = {"type": "conversation", "execution": "none"}

    # Voice approval is deliberately two-step: request, then a distinct spoken
    # confirmation within five minutes. The existing approval endpoint remains
    # the sole dispatcher and retains its product, RBAC and single-use checks.
    approval_request = re.fullmatch(r"(?:approve|review approval)\s+([0-9a-fA-F-]{20,})", transcript, re.I)
    if approval_request:
        if "approval.decide" not in p.permissions and "*" not in p.permissions:
            raise HTTPException(403, "Permission required: approval.decide")
        approval_id = approval_request.group(1)
        approvals = await repo.list_approvals()
        approval = next((item for item in approvals if str(item.get("id")) == approval_id), None)
        if not approval or str(approval.get("status", "")).upper() != "PENDING":
            raise HTTPException(404, "Pending approval not found in this tenant")
        authorize_product_access(p, approval.get("product_id"))
        await repo.session.execute(
            __import__("sqlalchemy").text("""
              UPDATE voice_sessions SET pending_approval_id=:approval,
                pending_approval_requested_at=now()
              WHERE id=:session AND tenant_id=:tenant AND principal_id=:principal
            """),
            {"approval": approval_id, "session": session_id,
             "tenant": p.tenant_id, "principal": p.user_id},
        )
        response = (
            f"Approval {approval_id} is pending. I have not executed it. "
            "Say 'confirm approval' or use the visual confirmation control to authorize this single action."
        )
        action = {"type": "approval_confirmation_required", "approval_id": approval_id,
                  "execution": "not_started"}
    elif re.fullmatch(r"(?:confirm approval|yes,? confirm approval|confirm it)", lowered):
        if "approval.decide" not in p.permissions and "*" not in p.permissions:
            raise HTTPException(403, "Permission required: approval.decide")
        row = (await repo.session.execute(
            __import__("sqlalchemy").text("""
              SELECT pending_approval_id, pending_approval_requested_at
              FROM voice_sessions
              WHERE id=:session AND tenant_id=:tenant AND principal_id=:principal
              FOR UPDATE
            """),
            {"session": session_id, "tenant": p.tenant_id, "principal": p.user_id},
        )).mappings().first()
        if not row or not row["pending_approval_id"] or not row["pending_approval_requested_at"]:
            raise HTTPException(409, "No pending voice approval confirmation exists")
        age = (datetime.now(timezone.utc) - row["pending_approval_requested_at"]).total_seconds()
        if age < 0 or age > 300:
            raise HTTPException(409, "Voice approval confirmation expired; request the approval again")
        approval_id = str(row["pending_approval_id"])
        result = await approve_single_use_outbox_action(
            approval_id,
            SingleUseApprovalRequest(reason="Founder explicitly confirmed through authenticated voice session"),
            p, repo,
        )
        await repo.session.execute(
            __import__("sqlalchemy").text("""
              UPDATE voice_sessions SET pending_approval_id=NULL,
                pending_approval_requested_at=NULL
              WHERE id=:session AND tenant_id=:tenant
            """), {"session": session_id, "tenant": p.tenant_id}
        )
        response = f"Your explicit approval was submitted. Governed dispatch status: {result.get('status', 'recorded')}."
        action = {"type": "approval_executed", "approval_id": approval_id,
                  "status": result.get("status"), "execution": "governed"}
    elif any(term in lowered for term in ("briefing", "company status", "executive status", "how are we doing", "revenue status", "capital status")):
        if "executive.read" not in p.permissions and "*" not in p.permissions:
            raise HTTPException(403, "Permission required: executive.read")
        snapshot = await _ceo_service(repo).briefing(generated_by=p.user_id)
        response = _spoken_executive_briefing(snapshot)
        action = {"type": "executive_briefing", "execution": "read_only"}
    else:
        if "work.assign" not in p.permissions and "*" not in p.permissions:
            raise HTTPException(403, "Permission required: work.assign")
        if not turn.get("product_id"):
            response = "I can prepare that directive, but this voice session has no product scope. Start a new session with the intended product selected."
            action = {"type": "scope_required", "execution": "not_started"}
        else:
            try:
                directive = await WorkforceOrchestrator(repo, p).submit_directive(
                    transcript, turn["product_id"]
                )
                response = (
                    f"Directive accepted by the Digital CEO for {turn['product_id']}. "
                    f"{directive.get('department_count', 0)} department work orders were created. "
                    "Specialists will prepare evidence; external actions remain approval-gated."
                )
                action = {"type": "directive_created", "directive_id": directive.get("id"),
                          "department_count": directive.get("department_count"),
                          "execution": "delegated_not_externally_dispatched"}
            except (PermissionError, ValueError) as exc:
                raise HTTPException(403 if isinstance(exc, PermissionError) else 422, str(exc)) from exc

    await _voice_sessions.append_response(repo, p, session_id, response)
    return {
        "session_id": session_id, "turn_id": turn["turn_id"],
        "response": response, "action": action,
        "interrupted_at_ms": turn["interrupted_at_ms"],
        "context_turn_count": len(turn["context"]),
        "identity_disclosure": AI_IDENTITY_DISCLOSURE,
    }


# Workforce 2.4 — tenant-scoped skill gym and governed social launch APIs.
from .skill_gym import SkillGymEngine
from .skill_registry import FORTUNE50_SKILLS
from sqlalchemy import text as sql_text

class SkillBuildAndTestRequest(BaseModel):
    skill_id: str = Field(min_length=3,max_length=120)
    department: str = Field(min_length=2,max_length=100)
    role_id: str = Field(min_length=2,max_length=100)
    category: str = Field(min_length=2,max_length=80)
    skill_name: str = Field(min_length=2,max_length=180)
    instructions: str = Field(min_length=20,max_length=12000)
    rubric_json: dict | None = None
    scenarios: list[dict] = Field(min_length=1,max_length=30)

class SocialPublishRequest(BaseModel):
    product_id: str = Field(min_length=2,max_length=120)
    channel: str = Field(min_length=2,max_length=40)
    content: str = Field(min_length=20,max_length=8000)
    utm: dict = Field(default_factory=dict)

@app.get("/v1/skills/catalog")
async def skill_catalog(p: Principal=Depends(require_permission("skills.read"))):
    return {"count":len(FORTUNE50_SKILLS),"skills":list(FORTUNE50_SKILLS)}

@app.post("/v1/skills/seed")
async def seed_skill_catalog(p: Principal=Depends(require_permission("skills.manage")),
                             repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    return await SkillGymEngine(repo,p).seed_catalog()

@app.post("/v1/skills/build-and-test")
async def build_and_test_skill(body: SkillBuildAndTestRequest,
    p: Principal=Depends(require_permission("skills.manage")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    if body.role_id not in BY_ID: raise HTTPException(404,"Unknown workforce role")
    role=BY_ID[body.role_id]
    if role.department!=body.department: raise HTTPException(422,"Skill department does not match role registry")
    if "*" not in p.permissions and "skills.manage" not in p.permissions:
        raise HTTPException(403,"Skill management permission required")
    engine=SkillGymEngine(repo,p)
    try:
        await engine.build_or_update_skill(skill_id=body.skill_id,department=body.department,
            role_id=body.role_id,category=body.category,skill_name=body.skill_name,
            instructions=body.instructions,rubric_json=body.rubric_json)
        return await engine.run_realtime_skill_test(body.skill_id,body.scenarios)
    except LookupError as exc: raise HTTPException(404,str(exc)) from exc
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc

@app.get("/v1/skills/{skill_id}/test-runs")
async def skill_test_runs(skill_id: str,p: Principal=Depends(require_permission("skills.read")),
                          repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    try: return await SkillGymEngine(repo,p).list_test_runs(skill_id)
    except LookupError as exc: raise HTTPException(404,str(exc)) from exc

@app.post("/v1/social/publish")
async def queue_social_publish(body: SocialPublishRequest,
    p: Principal=Depends(require_permission("campaign.publish")),
    repo: WorkforceRepositoryProtocol=Depends(get_workforce_repo)):
    authorize_product_access(p,body.product_id)
    if "social-publisher" not in BY_ID: raise HTTPException(503,"Social publisher role is unavailable")
    if not re.search(r"\\bAI\\b|artificial intelligence",body.content,re.I) or "RunGrow" not in body.content:
        raise HTTPException(422,"AI identity and RunGrow disclosure are mandatory")
    playbook=PRODUCT_PLAYBOOKS.get(body.product_id)
    if not playbook or playbook.get("stage")=="DISCOVERY_ONLY":
        raise HTTPException(403,"Product is not eligible for live campaign publishing")
    payload={"channel":body.channel.lower(),"content":body.content,"utm":body.utm,
             "product_id":body.product_id,"dispatch_payload":{"channel":body.channel.lower(),
             "content":body.content,"utm":body.utm,"product_id":body.product_id}}
    approval=await repo.create_approval(product_id=body.product_id,employee_id="social-publisher",
        action_type="social.publish",payload=payload,expires_at=datetime.now(timezone.utc)+timedelta(minutes=30))
    outbox_payload={"channel":body.channel.lower(),"dispatch_payload":payload["dispatch_payload"]}
    await repo.session.execute(sql_text("""INSERT INTO wf_outbox_events
      (id,tenant_id,product_id,directive_id,work_order_id,approval_id,employee_id,action_type,
       payload_hash,payload,status,created_at,updated_at)
      VALUES (:id,:tenant,:product,NULL,NULL,:approval,'social-publisher','social.publish',
       :hash,CAST(:payload AS jsonb),'QUEUED_FOR_APPROVAL',now(),now())"""),
      {"id":str(uuid4()),"tenant":p.tenant_id,"product":body.product_id,
       "approval":approval["id"],"hash":approval["payload_hash"],"payload":json.dumps(outbox_payload)})
    await repo.append_audit("social.publish_queued","outbox_event",str(approval["id"]),body.product_id,
        {"approval_id":approval["id"],"channel":body.channel.lower(),"status":"QUEUED_FOR_APPROVAL"})
    return {"approval_id":approval["id"],"status":"QUEUED_FOR_APPROVAL",
            "dispatch_status":"NOT_DISPATCHED","single_use_approval_required":True}
