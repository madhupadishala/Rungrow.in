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

app = FastAPI(title="RunGrow Workforce API", version="0.4.0")
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins,
    allow_credentials=True, allow_methods=["GET","POST"],
    allow_headers=["Authorization","Content-Type","Idempotency-Key","X-RunGrow-Signature","X-RunGrow-Timestamp","X-RunGrow-Tenant-ID","X-RunGrow-Product-ID","X-RunGrow-Org-ID","X-Correlation-ID"])

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
    tenant_id: str = Header(..., alias="X-RunGrow-Tenant-ID"),
    product_id: str = Header(..., alias="X-RunGrow-Product-ID"),
    org_id: str = Header(..., alias="X-RunGrow-Org-ID"),
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
    tenant_header: str | None = Header(default=None, alias="X-RunGrow-Tenant-ID"),
    product_header: str | None = Header(default=None, alias="X-RunGrow-Product-ID"),
    org_header: str | None = Header(default=None, alias="X-RunGrow-Org-ID"),
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
    secret = os.getenv("STUDIO_GROWTH_OS_SERVICE_SECRET", "")
    if not secret:
        raise HTTPException(503, "STUDIO_GROWTH_OS_SERVICE_SECRET is not configured")
    try:
        SignedWebhookVerifier(secret).verify(raw, signature, timestamp,
            tenant_id=tenant_id, product_id=product_id,
            workforce_tenant_id=internal_tenant_id)
        adapter = PhotoStudioGrowthOSAdapter(service_secret=secret)
        return await adapter.handle_webhook(body=raw, signature=signature, timestamp=timestamp,
            tenant_id=tenant_id, product_id=product_id, idempotency_key=idempotency_key,
            correlation_id=correlation_id, repository=repo)
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc



async def get_generic_adapter_repo(
    product_id: str,
    tenant_id: str = Header(..., alias="X-RunGrow-Tenant-ID"),
    org_id: str = Header(..., alias="X-RunGrow-Org-ID"),
):
    if product_id not in GENERIC_PRODUCT_IDS:
        raise HTTPException(404, "Unknown generic adapter product")
    internal_tenant_id = os.getenv("RUNGROW_INTERNAL_TENANT_ID", "")
    if not internal_tenant_id:
        raise HTTPException(503, "RUNGROW_INTERNAL_TENANT_ID is required for adapter isolation")
    if org_id != internal_tenant_id or tenant_id == internal_tenant_id:
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
    tenant_header: str | None = Header(default=None, alias="X-RunGrow-Tenant-ID"),
    product_header: str | None = Header(default=None, alias="X-RunGrow-Product-ID"),
    org_header: str | None = Header(default=None, alias="X-RunGrow-Org-ID"),
    correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if product_header != product_id:
        raise HTTPException(403, "Webhook product binding mismatch")
    if not tenant_header or not correlation_id or len(correlation_id) > 128 or not idempotency_key:
        raise HTTPException(400, "Tenant, correlation and idempotency headers are required")
    internal_tenant_id = os.getenv("RUNGROW_INTERNAL_TENANT_ID", "")
    if not internal_tenant_id or org_header != internal_tenant_id or tenant_header == internal_tenant_id:
        raise HTTPException(403, "Webhook tenant/org binding mismatch")
    raw = await request.body()
    adapter = PRODUCT_ADAPTERS.get(product_id)
    try:
        return await adapter.handle_webhook(raw_body=raw, signature=signature, timestamp=timestamp,
            external_tenant_id=tenant_header, workforce_tenant_id=internal_tenant_id,
            idempotency_key=idempotency_key, correlation_id=correlation_id, repository=repo)
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
