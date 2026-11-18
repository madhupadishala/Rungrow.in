from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import hashlib
import hmac
import json
import re

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .agent_runtime import AgentInput, AgentOrchestrator
from .config import settings
from .policy import Principal
from .registry import BY_ID, DEPARTMENTS, EMPLOYEES, public_registry
from .schemas import ApprovalDecision, ApprovalExecutionValidation, AgentRunRequest, CommandRequest, MemoryWrite, TaskCreate
from .security import current_principal, require_permission

app = FastAPI(title="RunGrow Workforce API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)

# Local-only adapter. Production must replace this with tenant-scoped PostgreSQL
# repositories and a durable outbox before multi-worker deployment.
STATE = {"approvals": [], "tasks": [], "audit": [], "memory": [], "runs": []}

_PORTFOLIO_PATH = Path(__file__).resolve().parents[2] / "data" / "workforce" / "product-portfolio.json"
with _PORTFOLIO_PATH.open("r", encoding="utf-8") as portfolio_file:
    _portfolio = json.load(portfolio_file)
PRODUCTS = {
    item["product_id"]: item
    for item in _portfolio.get("products", [])
    if isinstance(item, dict) and isinstance(item.get("product_id"), str)
}
PRODUCT_ROLE_IDS = {employee.product_id: employee.id for employee in EMPLOYEES if employee.product_id}


def validate_product_id(product_id: str | None) -> None:
    if product_id is not None and product_id not in PRODUCTS:
        raise HTTPException(status_code=404, detail="Unknown product scope")


def authorize_product_access(principal: Principal, product_id: str | None) -> None:
    validate_product_id(product_id)
    if product_id is not None and "*" not in principal.permissions and product_id not in principal.product_ids:
        raise HTTPException(status_code=403, detail="Principal is not authorised for this product")


def filter_product_scope(principal: Principal, rows: list[dict]) -> list[dict]:
    if "*" in principal.permissions:
        return rows
    return [row for row in rows if row.get("product_id") is None or row.get("product_id") in principal.product_ids]


def enforce_product_scope(employee, product_id: str | None) -> None:
    validate_product_id(product_id)
    if employee.product_id and product_id != employee.product_id:
        raise HTTPException(status_code=403, detail="Product-scoped employee requires its exact assigned product_id")


def _payload_hash(tenant_id: str, product_id: str | None, employee_id: str, action_type: str, payload: dict) -> str:
    canonical = json.dumps(
        {"tenant_id": tenant_id, "product_id": product_id, "employee_id": employee_id,
         "action_type": action_type, "payload": payload},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_approval(
    principal: Principal, *, product_id: str | None, employee_id: str,
    action_type: str, payload: dict, ttl: timedelta = timedelta(minutes=15),
) -> dict:
    authorize_product_access(principal, product_id)
    now = datetime.now(timezone.utc)
    item = {
        "id": str(uuid4()), "tenant_id": principal.tenant_id, "product_id": product_id,
        "employee_id": employee_id, "action_type": action_type, "payload": payload,
        "payload_hash": _payload_hash(principal.tenant_id, product_id, employee_id, action_type, payload),
        "requester_id": principal.user_id, "requested_by": principal.user_id,
        "approver_id": None, "status": "pending",
        "expires_at": (now + ttl).isoformat(), "created_at": now.isoformat(),
        "action": action_type,
    }
    STATE["approvals"].append(item)
    return item


def validate_approval_for_execution(
    principal: Principal, approval_id: str, *, product_id: str | None,
    employee_id: str, action_type: str, payload: dict,
) -> dict:
    item = next((row for row in STATE["approvals"]
                 if row["id"] == approval_id and row["tenant_id"] == principal.tenant_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    authorize_product_access(principal, product_id)
    if item["product_id"] != product_id:
        raise HTTPException(status_code=403, detail="Approval product scope mismatch")
    if item["employee_id"] != employee_id or item["action_type"] != action_type:
        raise HTTPException(status_code=403, detail="Approval action binding mismatch")
    expires_at = datetime.fromisoformat(item["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="Approval has expired")
    if item["status"] != "approved":
        raise HTTPException(status_code=409, detail="Approval is not approved")
    if not item.get("approver_id") or item["approver_id"] == item["requester_id"]:
        raise HTTPException(status_code=403, detail="Invalid approval separation of duties")
    stored_hash = _payload_hash(item["tenant_id"], item["product_id"], item["employee_id"], item["action_type"], item["payload"])
    supplied_hash = _payload_hash(item["tenant_id"], product_id, employee_id, action_type, payload)
    if not hmac.compare_digest(item["payload_hash"], stored_hash) or not hmac.compare_digest(item["payload_hash"], supplied_hash):
        raise HTTPException(status_code=409, detail="Approved payload integrity check failed")
    return item


def audit(principal: Principal, action: str, kind: str, resource_id: str, outcome: str, product_id: str | None = None) -> None:
    STATE["audit"].append({
        "id": str(uuid4()), "tenant_id": principal.tenant_id, "product_id": product_id,
        "actor_id": principal.user_id, "actor_type": principal.actor_type,
        "action": action, "resource_type": kind, "resource_id": resource_id,
        "outcome": outcome, "at": datetime.now(timezone.utc).isoformat(),
    })


def route(command_text: str):
    text = command_text.lower()
    mapping = [
        (("sales", "pipeline", "lead", "prospect", "deal"), ("Sales & Revenue", "sales-ops", "crm.read")),
        (("campaign", "seo", "content", "market"), ("Growth & Marketing", "market-intel", "research.public")),
        (("customer", "onboarding", "renewal", "adoption"), ("Customer Success", "cs-director", "customer.read")),
        (("support", "ticket", "issue"), ("Customer Support", "support-lead", "support.read")),
        (("product", "roadmap", "feature"), ("Product & Design", "pm", "product.read")),
        (("code", "engineering", "deploy", "bug"), ("Engineering & IT", "cto", "engineering.read")),
        (("invoice", "finance", "billing", "vendor"), ("Finance & Administration", "finance", "finance.read")),
        (("hiring", "candidate", "employee"), ("People & Culture", "people-ops", "people.read")),
        (("process", "operations"), ("Business Operations", "bizops", "operations.read")),
        (("contract", "privacy", "compliance", "legal"), ("Legal, Risk & Compliance", "legal", "compliance.read")),
    ]
    for words, result in mapping:
        if any(word in text for word in words):
            return result
    return ("Executive Office", "chief-staff", "reports.read")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "rungrow-workforce-api", "adapter": "local-memory"}


@app.get("/v1/workforce/registry")
async def registry(p: Principal = Depends(require_permission("workforce.read"))):
    return {"departments": list(DEPARTMENTS), "employees": public_registry(), "count": len(EMPLOYEES)}


@app.post("/v1/workforce/commands")
async def command(body: CommandRequest, p: Principal = Depends(current_principal)):
    authorize_product_access(p, body.product_id)
    department, role_id, permission = route(body.command)
    if body.product_id:
        role_id = PRODUCT_ROLE_IDS[body.product_id]
    role = BY_ID[role_id]
    enforce_product_scope(role, body.product_id)
    command_id = str(uuid4())
    if permission not in p.permissions and "*" not in p.permissions:
        audit(p, "command.route", "command", command_id, "denied", body.product_id)
        return {"status": "denied", "message": "Your workspace role is not authorised for this department data.", "command_id": command_id}
    if re.search(r"\b(send|launch|activate|charge|refund|delete|export|deploy|sign|purchase|discount|change price)\b", body.command, re.I):
        approval = create_approval(p, product_id=body.product_id, employee_id=role.id,
                                   action_type="command.execution",
                                   payload={"command": body.command, "role_id": role_id})
        audit(p, "command.approval_requested", "approval", approval["id"], "pending", body.product_id)
        return {"status": "approval_required", "message": role.title + " is the proposed owner. Human approval is required before execution.",
                "command_id": command_id, "approval_id": approval["id"], "routed_department": department}
    audit(p, "command.accepted", "command", command_id, "accepted", body.product_id)
    return {"status": "accepted", "message": "Routed to " + role.title + " in " + department + ". Accepted for planning; no external action has been performed.",
            "command_id": command_id, "routed_department": department, "routed_role_id": role_id}


@app.post("/v1/workforce/tasks")
async def create_task(body: TaskCreate, p: Principal = Depends(require_permission("work.assign"))):
    validate_product_id(body.product_id)
    if body.assignee_role_id not in BY_ID:
        raise HTTPException(status_code=404, detail="Unknown employee role")
    assignee = BY_ID[body.assignee_role_id]
    enforce_product_scope(assignee, body.product_id)
    item = {"id": str(uuid4()), "tenant_id": p.tenant_id, "title": body.title,
            "description": body.description, "assignee_role_id": body.assignee_role_id,
            "product_id": body.product_id, "priority": body.priority, "status": "queued",
            "created_by": p.user_id}
    STATE["tasks"].append(item)
    audit(p, "task.create", "task", item["id"], "created", body.product_id)
    return item


@app.get("/v1/workforce/tasks")
async def tasks(product_id: str | None = None, p: Principal = Depends(require_permission("work.read"))):
    authorize_product_access(p, product_id)
    rows = [row for row in STATE["tasks"] if row["tenant_id"] == p.tenant_id]
    rows = filter_product_scope(p, rows)
    return [row for row in rows if row.get("product_id") == product_id] if product_id else rows


@app.get("/v1/workforce/approvals")
async def approvals(product_id: str | None = None, p: Principal = Depends(require_permission("approval.read"))):
    authorize_product_access(p, product_id)
    rows = [row for row in STATE["approvals"] if row["tenant_id"] == p.tenant_id]
    rows = filter_product_scope(p, rows)
    return [row for row in rows if row.get("product_id") == product_id] if product_id else rows


@app.post("/v1/workforce/approvals/{approval_id}/decision")
async def decide(approval_id: str, body: ApprovalDecision, p: Principal = Depends(require_permission("approval.decide"))):
    item = next((row for row in STATE["approvals"]
                 if row["id"] == approval_id and row["tenant_id"] == p.tenant_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    authorize_product_access(p, item["product_id"])
    if body.product_id != item["product_id"]:
        raise HTTPException(status_code=403, detail="Approval product scope mismatch")
    if item["status"] != "pending":
        raise HTTPException(status_code=409, detail="Approval already decided")
    if item["requester_id"] == p.user_id:
        raise HTTPException(status_code=403, detail="Separation of duties: requester cannot approve their own request")
    expires_at = datetime.fromisoformat(item["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="Approval has expired")
    item.update({"status": body.decision, "approver_id": p.user_id, "decided_by": p.user_id,
                 "reason": body.reason, "decided_at": datetime.now(timezone.utc).isoformat()})
    audit(p, "approval.decision", "approval", approval_id, body.decision, item["product_id"])
    return {"id": approval_id, "status": item["status"], "execution": "not_started"}


@app.post("/v1/workforce/approvals/{approval_id}/validate-execution")
async def validate_execution(approval_id: str, body: ApprovalExecutionValidation,
                             p: Principal = Depends(require_permission("approval.execute"))):
    validate_product_id(body.product_id)
    item = validate_approval_for_execution(p, approval_id, product_id=body.product_id,
                                           employee_id=body.employee_id, action_type=body.action_type,
                                           payload=body.payload)
    audit(p, "approval.execution_validated", "approval", approval_id, "authorised", body.product_id)
    return {"approval_id": item["id"], "authorised": True, "execution": "not_started"}


@app.post("/v1/workforce/memory")
async def memory(body: MemoryWrite, p: Principal = Depends(require_permission("memory.write"))):
    validate_product_id(body.product_id)
    if body.role_id not in BY_ID:
        raise HTTPException(status_code=404, detail="Unknown role")
    employee = BY_ID[body.role_id]
    enforce_product_scope(employee, body.product_id)
    item = {"id": str(uuid4()), "tenant_id": p.tenant_id, "role_id": body.role_id,
            "product_id": body.product_id, "department": employee.department,
            "content": body.content, "source_ref": body.source_ref, "retention_days": body.retention_days}
    STATE["memory"].append(item)
    audit(p, "memory.write", "memory", item["id"], "created", body.product_id)
    return {"id": item["id"], "scope": "tenant+department+role"}


@app.get("/v1/workforce/memory")
async def read_memory(product_id: str | None = None, p: Principal = Depends(require_permission("memory.read"))):
    authorize_product_access(p, product_id)
    rows = [row for row in STATE["memory"] if row["tenant_id"] == p.tenant_id]
    rows = filter_product_scope(p, rows)
    return [row for row in rows if row.get("product_id") == product_id] if product_id else rows


@app.get("/v1/workforce/audit")
async def audit_events(product_id: str | None = None, p: Principal = Depends(require_permission("audit.read"))):
    authorize_product_access(p, product_id)
    rows = [row for row in STATE["audit"] if row["tenant_id"] == p.tenant_id]
    rows = filter_product_scope(p, rows)
    if product_id is not None:
        rows = [row for row in rows if row.get("product_id") == product_id]
    return rows[-500:]


@app.get("/v1/workforce/agent-runs")
async def agent_runs(product_id: str | None = None, p: Principal = Depends(require_permission("work.read"))):
    authorize_product_access(p, product_id)
    rows = [row for row in STATE["runs"] if row["tenant_id"] == p.tenant_id]
    rows = filter_product_scope(p, rows)
    return [row for row in rows if row.get("product_id") == product_id] if product_id else rows


@app.post("/v1/workforce/agent-runs")
async def run_employee(body: AgentRunRequest, p: Principal = Depends(require_permission("agent.run"))):
    validate_product_id(body.product_id)
    employee = BY_ID.get(body.role_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Unknown workforce employee")
    enforce_product_scope(employee, body.product_id)
    missing = set(employee.permissions) - p.permissions
    if "*" not in p.permissions and missing:
        raise HTTPException(status_code=403, detail="Invoker does not hold the employee tool scopes required for this run")
    plan = await AgentOrchestrator().run(employee, AgentInput(p.tenant_id, p.user_id, body.task, tuple(body.source_refs)))
    product_id = employee.product_id or body.product_id
    record = {"id": str(uuid4()), "tenant_id": p.tenant_id, "employee_id": employee.id,
              "product_id": product_id, "requested_by": p.user_id, "task": body.task,
              "status": plan.status, "summary": plan.summary, "steps": list(plan.steps),
              "approval_required": plan.approval_required, "runtime_mode": plan.runtime_mode,
              "created_at": datetime.now(timezone.utc).isoformat()}
    STATE["runs"].append(record)
    if plan.approval_required:
        approval = create_approval(
            p, product_id=product_id, employee_id=employee.id, action_type="agent.proposed_action",
            payload={"agent_run_id": record["id"], "employee_id": employee.id, "product_id": product_id,
                     "task": body.task, "proposed_actions": list(plan.proposed_actions)},
        )
        record["approval_id"] = approval["id"]
        audit(p, "agent.approval_requested", "approval", approval["id"], "pending", product_id)
    audit(p, "agent.run.plan", "agent_run", record["id"], plan.status, product_id)
    return record
