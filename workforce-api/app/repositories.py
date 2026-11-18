"""Tenant-scoped async PostgreSQL repositories for RunGrow Workforce.

All methods execute inside a transaction that installs verified Principal scope
with SET LOCAL. No repository method accepts tenant_id from request data.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .policy import Principal

@lru_cache(maxsize=1)
def session_factory():
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for Workforce API runtime")
    url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class TenantRepository:
    def __init__(self, session: AsyncSession, principal: Principal):
        self.session = session
        self.principal = principal

    async def __aenter__(self):
        await self.session.begin()
        await self.session.execute(
            text("SELECT set_config('app.current_tenant_id', :tenant, true)"),
            {"tenant": self.principal.tenant_id},
        )
        await self.session.execute(
            text("SELECT set_config('app.product_ids', :products, true)"),
            {"products": ",".join(sorted(self.principal.product_ids))},
        )
        await self.session.execute(
            text("SELECT set_config('app.current_principal_id', :principal, true)"),
            {"principal": self.principal.user_id},
        )
        await self.session.execute(
            text("SELECT set_config('app.is_company_admin', :admin, true)"),
            {"admin": "true" if "*" in self.principal.permissions else "false"},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            await self.session.rollback()
        else:
            await self.session.commit()

    async def _rows(self, sql: str, params: dict[str, Any] | None = None):
        result = await self.session.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings().all()]

    async def _one(self, sql: str, params: dict[str, Any] | None = None):
        result = await self.session.execute(text(sql), params or {})
        row = result.mappings().first()
        return dict(row) if row else None

    @staticmethod
    def _product(product_id: str | None):
        if product_id is not None and (not isinstance(product_id, str) or not product_id.strip()):
            raise ValueError("Invalid product scope")

    async def create_task(self, *, product_id: str | None, department: str, assignee_role_id: str,
                          title: str, description: str, priority: str, idempotency_key: str):
        self._product(product_id)
        item = await self._one("""
          INSERT INTO wf_tasks
            (id, tenant_id, product_id, department, assignee_role_id, title, description,
             priority, status, created_by, idempotency_key, created_at)
          VALUES (:id, :tenant, :product, :department, :assignee, :title, :description,
                  :priority, 'queued', :actor, :idem, now())
          ON CONFLICT (tenant_id, idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
          RETURNING *
        """, {"id": str(uuid4()), "tenant": self.principal.tenant_id, "product": product_id,
              "department": department, "assignee": assignee_role_id, "title": title,
              "description": description, "priority": priority, "actor": self.principal.user_id,
              "idem": idempotency_key})
        if item:
            await self.append_audit("task.create", "task", str(item["id"]), product_id, {"idempotency_key": idempotency_key})
        return item

    async def list_tasks(self, product_id: str | None = None):
        self._product(product_id)
        return await self._rows("""
          SELECT * FROM wf_tasks
          WHERE tenant_id=:tenant AND (:product IS NULL OR product_id=:product)
          ORDER BY created_at DESC
        """, {"tenant": self.principal.tenant_id, "product": product_id})

    async def create_agent_run(self, *, product_id: str | None, employee_id: str, task: str,
                               status: str, summary: str, steps: list, correlation_id: str):
        self._product(product_id)
        item = await self._one("""
          INSERT INTO wf_agent_runs
            (id, tenant_id, product_id, employee_id, requested_by, task, status, summary,
             steps, correlation_id, created_at)
          VALUES (:id,:tenant,:product,:employee,:actor,:task,:status,:summary,
                  CAST(:steps AS jsonb),:correlation,now()) RETURNING *
        """, {"id": str(uuid4()), "tenant": self.principal.tenant_id, "product": product_id,
              "employee": employee_id, "actor": self.principal.user_id, "task": task,
              "status": status, "summary": summary, "steps": json.dumps(steps),
              "correlation": correlation_id})
        if item:
            await self.append_audit("agent.run", "agent_run", str(item["id"]), product_id, {"correlation_id": correlation_id})
        return item

    async def transition_agent_run(self, run_id: str, expected_status: str, new_status: str):
        return await self._one("""
          UPDATE wf_agent_runs SET status=:new_status, updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND status=:expected
          RETURNING *
        """, {"id": run_id, "tenant": self.principal.tenant_id,
              "expected": expected_status, "new_status": new_status})

    async def list_agent_runs(self, product_id: str | None = None):
        self._product(product_id)
        return await self._rows("""
          SELECT * FROM wf_agent_runs WHERE tenant_id=:tenant
          AND (:product IS NULL OR product_id=:product) ORDER BY created_at DESC
        """, {"tenant": self.principal.tenant_id, "product": product_id})

    async def write_memory(self, *, product_id: str | None, role_id: str, department: str,
                           content: str, source_ref: str, retention_days: int, embedding: list[float] | None = None):
        self._product(product_id)
        vector = "[" + ",".join(str(float(v)) for v in embedding) + "]" if embedding else None
        item = await self._one("""
          INSERT INTO wf_memory
            (id, tenant_id, product_id, role_id, department, content, source_ref,
             retention_days, embedding, created_at)
          VALUES (:id,:tenant,:product,:role,:department,:content,:source,:retention,
                  CAST(:embedding AS vector),now()) RETURNING id, tenant_id, product_id, role_id, department, content, source_ref, retention_days, created_at
        """, {"id": str(uuid4()), "tenant": self.principal.tenant_id, "product": product_id,
              "role": role_id, "department": department, "content": content,
              "source": source_ref, "retention": retention_days, "embedding": vector})
        if item:
            await self.append_audit("memory.write", "memory", str(item["id"]), product_id, {"source_ref": source_ref})
        return item

    async def list_memory(self, product_id: str | None = None):
        self._product(product_id)
        return await self._rows("""
          SELECT id,tenant_id,product_id,role_id,department,content,source_ref,retention_days,created_at
          FROM wf_memory WHERE tenant_id=:tenant AND (:product IS NULL OR product_id=:product)
          ORDER BY created_at DESC
        """, {"tenant": self.principal.tenant_id, "product": product_id})

    async def create_approval(self, *, product_id: str | None, employee_id: str, action_type: str,
                              payload: dict, expires_at: datetime):
        self._product(product_id)
        digest = hashlib.sha256(json.dumps(
            {"tenant_id": self.principal.tenant_id, "product_id": product_id,
             "employee_id": employee_id, "action_type": action_type, "payload": payload},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        approval_id = str(uuid4())
        item = await self._one("""
          INSERT INTO wf_approvals
            (id,tenant_id,product_id,employee_id,action_type,payload,payload_hash,
             requester_id,status,expires_at,created_at)
          VALUES (:id,:tenant,:product,:employee,:action,CAST(:payload AS jsonb),:hash,
                  :requester,'pending',:expires,now()) RETURNING *
        """, {"id": approval_id, "tenant": self.principal.tenant_id, "product": product_id,
              "employee": employee_id, "action": action_type, "payload": json.dumps(payload),
              "hash": digest, "requester": self.principal.user_id, "expires": expires_at})
        await self.append_audit("approval.requested", "approval", approval_id, product_id,
                                {"payload_hash": digest})
        return item

    async def decide_approval(self, approval_id: str, decision: str, reason: str, product_id: str | None = None):
        if decision not in {"approved", "rejected"}:
            raise ValueError("Invalid approval decision")
        item = await self._one("""
          UPDATE wf_approvals SET status=:decision, approver_id=:approver,
                 decision_reason=:reason, decided_at=now()
          WHERE id=:id AND tenant_id=:tenant AND product_id IS NOT DISTINCT FROM :product
            AND status='pending' AND requester_id <> :approver AND expires_at > now()
          RETURNING *
        """, {"decision": decision, "approver": self.principal.user_id, "reason": reason,
              "id": approval_id, "tenant": self.principal.tenant_id, "product": product_id})
        if not item:
            raise PermissionError("Approval is unavailable, expired, already decided, or separation of duties failed")
        await self.append_audit("approval." + decision, "approval", approval_id,
                                item["product_id"], {"reason": reason})
        return item

    async def validate_approval(self, approval_id: str, *, product_id: str | None,
                                employee_id: str, action_type: str, payload: dict):
        self._product(product_id)
        item = await self._one("""
          SELECT * FROM wf_approvals WHERE id=:id AND tenant_id=:tenant
            AND product_id IS NOT DISTINCT FROM :product AND employee_id=:employee
            AND action_type=:action AND status='approved' AND expires_at > now()
            AND approver_id IS NOT NULL AND approver_id <> requester_id
          FOR UPDATE
        """, {"id": approval_id, "tenant": self.principal.tenant_id, "product": product_id,
              "employee": employee_id, "action": action_type})
        if not item:
            raise PermissionError("Approval validation failed")
        digest = hashlib.sha256(json.dumps(
            {"tenant_id": self.principal.tenant_id, "product_id": product_id,
             "employee_id": employee_id, "action_type": action_type, "payload": payload},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if not __import__("hmac").compare_digest(item["payload_hash"], digest):
            raise PermissionError("Approved payload does not match execution payload")
        return item

    async def list_approvals(self, product_id: str | None = None):
        self._product(product_id)
        return await self._rows("""
          SELECT * FROM wf_approvals WHERE tenant_id=:tenant
          AND (:product IS NULL OR product_id=:product) ORDER BY created_at DESC
        """, {"tenant": self.principal.tenant_id, "product": product_id})

    async def list_audit(self, product_id: str | None = None):
        self._product(product_id)
        return await self._rows("""
          SELECT * FROM wf_audit_events WHERE tenant_id=:tenant
          AND (:product IS NULL OR product_id=:product) ORDER BY occurred_at DESC
        """, {"tenant": self.principal.tenant_id, "product": product_id})

    async def append_audit(self, action: str, resource_type: str, resource_id: str,
                           product_id: str | None, details: dict):
        await self.session.execute(text("""
          INSERT INTO wf_audit_events
            (id,tenant_id,product_id,actor_id,action,resource_type,resource_id,details,occurred_at)
          VALUES (:id,:tenant,:product,:actor,:action,:type,:resource,CAST(:details AS jsonb),now())
        """), {"id": str(uuid4()), "tenant": self.principal.tenant_id, "product": product_id,
               "actor": self.principal.user_id, "action": action, "type": resource_type,
               "resource": resource_id, "details": json.dumps(details)})


async def repository_for(principal: Principal):
    SessionFactory = session_factory()
    async with SessionFactory() as session:
        async with TenantRepository(session, principal) as repository:
            yield repository


from typing import Protocol, runtime_checkable

@runtime_checkable
class WorkforceRepositoryProtocol(Protocol):
    async def create_task(self, **kwargs): ...
    async def list_tasks(self, product_id=None): ...
    async def create_agent_run(self, **kwargs): ...
    async def list_agent_runs(self, product_id=None): ...
    async def write_memory(self, **kwargs): ...
    async def list_memory(self, product_id=None): ...
    async def create_approval(self, **kwargs): ...
    async def list_approvals(self, product_id=None): ...
    async def decide_approval(self, approval_id, decision, reason, product_id=None): ...
    async def validate_approval(self, approval_id, **kwargs): ...
    async def append_audit(self, action, resource_type, resource_id, product_id, details): ...
    async def list_audit(self, product_id=None): ...


class InMemoryWorkforceRepository:
    """Explicitly injected, per-app test adapter. Never selected by production."""
    def __init__(self, principal: Principal, products: set[str]):
        self.principal, self.products = principal, products
        self.tasks, self.runs, self.memory, self.approvals, self.audit = [], [], [], [], []

    def _scope(self, product_id):
        if product_id is not None and product_id not in self.products:
            raise ValueError("Unknown product scope")
        if product_id is not None and "*" not in self.principal.permissions and product_id not in self.principal.product_ids:
            raise PermissionError("Product entitlement denied")

    def _audit(self, action, kind, rid, product_id, details=None):
        self.audit.append({"id": str(uuid4()), "tenant_id": self.principal.tenant_id,
            "product_id": product_id, "actor_id": self.principal.user_id, "action": action,
            "resource_type": kind, "resource_id": rid, "details": details or {},
            "occurred_at": datetime.now(timezone.utc).isoformat()})

    async def create_task(self, **kw):
        product=kw["product_id"]; self._scope(product)
        key=kw["idempotency_key"]
        old=next((x for x in self.tasks if x["idempotency_key"]==key),None)
        if old: return old
        item={"id":str(uuid4()),"tenant_id":self.principal.tenant_id,**kw,"status":"queued",
              "created_by":self.principal.user_id,"created_at":datetime.now(timezone.utc).isoformat()}
        self.tasks.append(item); self._audit("task.create","task",item["id"],product); return item

    async def list_tasks(self, product_id=None):
        self._scope(product_id)
        return [x for x in self.tasks if x["tenant_id"]==self.principal.tenant_id
                and x.get("product_id")==product_id] if product_id is not None else [
                    x for x in self.tasks if x["tenant_id"]==self.principal.tenant_id
                    and (x.get("product_id") is None or "*" in self.principal.permissions or x.get("product_id") in self.principal.product_ids)]

    async def create_agent_run(self, **kw):
        self._scope(kw["product_id"])
        item={"id":str(uuid4()),"tenant_id":self.principal.tenant_id,"requested_by":self.principal.user_id,
              **kw,"created_at":datetime.now(timezone.utc).isoformat()}
        self.runs.append(item); self._audit("agent.run","agent_run",item["id"],kw["product_id"]); return item

    async def list_agent_runs(self, product_id=None):
        self._scope(product_id)
        return [x for x in self.runs if x["tenant_id"]==self.principal.tenant_id and
            (x.get("product_id")==product_id if product_id else
             x.get("product_id") is None or "*" in self.principal.permissions or x.get("product_id") in self.principal.product_ids)]

    async def write_memory(self, **kw):
        self._scope(kw["product_id"])
        item={"id":str(uuid4()),"tenant_id":self.principal.tenant_id,**kw}
        self.memory.append(item); self._audit("memory.write","memory",item["id"],kw["product_id"]); return item

    async def list_memory(self, product_id=None):
        self._scope(product_id)
        return [x for x in self.memory if x["tenant_id"]==self.principal.tenant_id and
            (x.get("product_id")==product_id if product_id else
             x.get("product_id") is None or "*" in self.principal.permissions or x.get("product_id") in self.principal.product_ids)]

    async def create_approval(self, **kw):
        self._scope(kw["product_id"])
        digest=hashlib.sha256(json.dumps({"tenant_id":self.principal.tenant_id,"product_id":kw["product_id"],
          "employee_id":kw["employee_id"],"action_type":kw["action_type"],"payload":kw["payload"]},
          sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
        item={"id":str(uuid4()),"tenant_id":self.principal.tenant_id,"requester_id":self.principal.user_id,
          "approver_id":None,"status":"pending","payload_hash":digest,"created_at":datetime.now(timezone.utc).isoformat(),**kw}
        self.approvals.append(item); self._audit("approval.requested","approval",item["id"],kw["product_id"]); return item

    async def list_approvals(self, product_id=None):
        self._scope(product_id)
        return [x for x in self.approvals if x["tenant_id"]==self.principal.tenant_id and
          (x.get("product_id")==product_id if product_id else
           x.get("product_id") is None or "*" in self.principal.permissions or x.get("product_id") in self.principal.product_ids)]

    async def decide_approval(self, approval_id, decision, reason):
        item=next((x for x in self.approvals if x["id"]==approval_id and x["tenant_id"]==self.principal.tenant_id),None)
        if not item: raise LookupError("Approval not found")
        if item["product_id"] != product_id: raise PermissionError("Approval product scope mismatch")
        if item["requester_id"]==self.principal.user_id: raise PermissionError("Self approval forbidden")
        if item["status"]!="pending" or item["expires_at"]<=datetime.now(timezone.utc): raise PermissionError("Approval expired or already decided")
        item.update(status=decision,approver_id=self.principal.user_id,decision_reason=reason,decided_at=datetime.now(timezone.utc))
        self._audit("approval."+decision,"approval",approval_id,item["product_id"]); return item

    async def validate_approval(self, approval_id, **kw):
        item=next((x for x in self.approvals if x["id"]==approval_id and x["tenant_id"]==self.principal.tenant_id),None)
        if not item: raise LookupError("Approval not found")
        if item["status"]!="approved" or item["expires_at"]<=datetime.now(timezone.utc) or item["approver_id"]==item["requester_id"]:
            raise PermissionError("Approval validation failed")
        for k in ("product_id","employee_id","action_type"):
            if item[k]!=kw[k]: raise PermissionError("Approval binding mismatch")
        expected=hashlib.sha256(json.dumps({"tenant_id":self.principal.tenant_id,"product_id":kw["product_id"],
          "employee_id":kw["employee_id"],"action_type":kw["action_type"],"payload":kw["payload"]},
          sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
        if not __import__("hmac").compare_digest(item["payload_hash"],expected): raise PermissionError("Payload integrity failure")
        return item

    async def append_audit(self, action, resource_type, resource_id, product_id, details):
        self._scope(product_id)
        self._audit(action,resource_type,resource_id,product_id,details)

    async def list_audit(self, product_id=None):
        self._scope(product_id)
        return [x for x in self.audit if x["tenant_id"]==self.principal.tenant_id and
          (x.get("product_id")==product_id if product_id else
           x.get("product_id") is None or "*" in self.principal.permissions or x.get("product_id") in self.principal.product_ids)]

