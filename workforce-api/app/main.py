from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import json
import re

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .agent_runtime import AgentInput, AgentOrchestrator
from .config import settings
from .policy import Principal
from .registry import BY_ID, DEPARTMENTS, EMPLOYEES, public_registry
from .repositories import WorkforceRepositoryProtocol, TenantRepository, session_factory
from .schemas import ApprovalDecision, ApprovalExecutionValidation, AgentRunRequest, CommandRequest, MemoryWrite, TaskCreate
from .security import current_principal, require_permission

app = FastAPI(title="RunGrow Workforce API", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins,
    allow_credentials=True, allow_methods=["GET","POST"],
    allow_headers=["Authorization","Content-Type","Idempotency-Key"])

_PORTFOLIO_PATH = Path(__file__).resolve().parents[2] / "data" / "workforce" / "product-portfolio.json"
with _PORTFOLIO_PATH.open("r", encoding="utf-8") as f:
    _portfolio = json.load(f)
PRODUCTS = {x["product_id"]: x for x in _portfolio.get("products", [])}
PRODUCT_ROLE_IDS = {e.product_id:e.id for e in EMPLOYEES if e.product_id}
if set(PRODUCT_ROLE_IDS) != set(PRODUCTS):
    raise RuntimeError("Product portfolio and Product Growth Manager registries do not match")


@app.on_event("startup")
async def require_database_configuration():
    if not settings.database_url and settings.app_env != "test":
        raise RuntimeError("DATABASE_URL is required; Workforce API refuses volatile in-memory production storage")


async def get_workforce_repo(p: Principal = Depends(current_principal)):
    if not settings.database_url:
        raise HTTPException(503, "DATABASE_URL is not configured")
    factory = session_factory()
    async with factory() as session:
        async with TenantRepository(session, p) as repository:
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
    if re.search(r"\\b(send|launch|activate|charge|refund|delete|export|deploy|sign|purchase|discount|change price)\\b",body.command,re.I):
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
            priority=body.priority,idempotency_key=idempotency_key or str(uuid4()))
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
            status=plan.status,summary=plan.summary,steps=list(plan.steps),correlation_id=str(uuid4()))
        if plan.approval_required:
            approval=await repo.create_approval(product_id=product_id,employee_id=employee.id,
                action_type="agent.proposed_action",payload={"agent_run_id":str(record["id"]),
                "employee_id":employee.id,"product_id":product_id,"task":body.task,
                "proposed_actions":list(plan.proposed_actions)},
                expires_at=datetime.now(timezone.utc)+timedelta(minutes=15))
            record["approval_id"]=approval["id"]
        return record
    except Exception as exc: translate_repo_error(exc)
