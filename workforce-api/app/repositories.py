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


class PostgresWorkforceRepository:
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
                          title: str, description: str, priority: str, idempotency_key: str,
                          is_sandbox: bool=False, max_attempts: int=5):
        self._product(product_id)
        item = await self._one("""
          INSERT INTO wf_tasks
            (id, tenant_id, product_id, department, assignee_role_id, title, description,
             priority, status, created_by, idempotency_key, created_at, is_sandbox, max_attempts)
          VALUES (:id, :tenant, :product, :department, :assignee, :title, :description,
                  :priority, 'QUEUED', :actor, :idem, now(), :sandbox, :max_attempts)
          ON CONFLICT (tenant_id, idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
          RETURNING *
        """, {"id": str(uuid4()), "tenant": self.principal.tenant_id, "product": product_id,
              "department": department, "assignee": assignee_role_id, "title": title,
              "description": description, "priority": priority, "actor": self.principal.user_id,
              "idem": idempotency_key, "sandbox": is_sandbox, "max_attempts": max_attempts})
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
                               status: str, summary: str, steps: list, correlation_id: str, is_sandbox: bool=False):
        self._product(product_id)
        item = await self._one("""
          INSERT INTO wf_agent_runs
            (id, tenant_id, product_id, employee_id, requested_by, task, status, summary,
             steps, correlation_id, created_at, is_sandbox, execution_state)
          VALUES (:id,:tenant,:product,:employee,:actor,:task,:status,:summary,
                  CAST(:steps AS jsonb),:correlation,now(),:sandbox,'QUEUED') RETURNING *
        """, {"id": str(uuid4()), "tenant": self.principal.tenant_id, "product": product_id,
              "employee": employee_id, "actor": self.principal.user_id, "task": task,
              "status": status, "summary": summary, "steps": json.dumps(steps),
              "correlation": correlation_id, "sandbox": is_sandbox})
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

    async def get_agent_run(self, run_id: str):
        return await self._one("SELECT * FROM wf_agent_runs WHERE id=:id AND tenant_id=:tenant",
            {"id":run_id,"tenant":self.principal.tenant_id})

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

    async def commit_boundary(self):
        await self.session.commit()
        await self.session.begin()
        await self.session.execute(text("SELECT set_config('app.current_tenant_id', :tenant, true)"),{"tenant":self.principal.tenant_id})
        await self.session.execute(text("SELECT set_config('app.product_ids', :products, true)"),{"products":",".join(sorted(self.principal.product_ids))})
        await self.session.execute(text("SELECT set_config('app.current_principal_id', :principal, true)"),{"principal":self.principal.user_id})
        await self.session.execute(text("SELECT set_config('app.is_company_admin', :admin, true)"),{"admin":"true" if "*" in self.principal.permissions else "false"})

    async def resume_agent_run(self, run_id, approval_id):
        item=await self._one("""
          UPDATE wf_agent_runs r SET status='planned',execution_state='QUEUED',updated_at=now()
          WHERE r.id=:run AND r.tenant_id=:tenant AND r.execution_state='AWAITING_APPROVAL'
            AND EXISTS (SELECT 1 FROM wf_approvals a WHERE a.id=:approval AND a.tenant_id=r.tenant_id
              AND a.product_id IS NOT DISTINCT FROM r.product_id AND a.employee_id=r.employee_id
              AND a.status='approved' AND a.expires_at>now() AND a.consumed_at IS NULL)
          RETURNING r.*
        """,{"run":run_id,"tenant":self.principal.tenant_id,"approval":approval_id})
        if not item: raise PermissionError("Run cannot resume without matching live approval")
        await self.append_audit("agent_run.resumed","agent_run",run_id,item["product_id"],{"approval_id":approval_id})
        return item

    async def record_model_usage(self, **usage):
        await self.session.execute(text("""
          INSERT INTO wf_model_usage(id,tenant_id,product_id,employee_id,run_id,provider,model,
            prompt_tokens,completion_tokens,estimated_cost_inr,latency_ms,created_at)
          VALUES(:id,:tenant,:product,:employee,:run,:provider,:model,:prompt,:completion,:cost,:latency,now())
        """),{"id":str(uuid4()),"tenant":self.principal.tenant_id,"product":usage["product_id"],
          "employee":usage["employee_id"],"run":usage["run_id"],"provider":usage["provider"],
          "model":usage["model"],"prompt":usage["prompt_tokens"],"completion":usage["completion_tokens"],
          "cost":usage["estimated_cost_inr"],"latency":usage["latency_ms"]})

    async def claim_task(self, worker_id: str, lease_seconds: int, product_id: str | None=None):
        return await self._one("""
          WITH candidate AS (
            SELECT id FROM wf_tasks
            WHERE ((status IN ('QUEUED','RETRY_SCHEDULED') AND next_retry_at <= now())
              OR (status='RUNNING' AND lease_expires_at <= now()))
              AND tenant_id=:tenant AND (:requested_product IS NULL OR product_id=:requested_product)
              AND (product_id IS NULL OR product_id=ANY(string_to_array(:products,',')) OR :admin='true')
            ORDER BY next_retry_at,created_at
            FOR UPDATE SKIP LOCKED LIMIT 1
          )
          UPDATE wf_tasks t SET
            status=CASE WHEN t.attempt_count >= t.max_attempts THEN 'DEAD_LETTER' ELSE 'RUNNING' END,
            locked_by=CASE WHEN t.attempt_count >= t.max_attempts THEN NULL ELSE :worker END,
            lease_expires_at=CASE WHEN t.attempt_count >= t.max_attempts THEN NULL ELSE now()+make_interval(secs=>:lease) END,
            attempt_count=CASE WHEN t.attempt_count >= t.max_attempts THEN t.attempt_count ELSE t.attempt_count+1 END,
            updated_at=now()
          FROM candidate c WHERE t.id=c.id AND t.tenant_id=:tenant
          RETURNING t.*
        """,{"tenant":self.principal.tenant_id,"products":",".join(self.principal.product_ids),
             "admin":"true" if "*" in self.principal.permissions else "false",
             "worker":worker_id,"lease":lease_seconds,"requested_product":product_id})

    async def finish_task(self, task_id: str, worker_id: str, result: Any):
        item=await self._one("""
          UPDATE wf_tasks SET status='COMPLETED',result=CAST(:result AS jsonb),
            locked_by=NULL,lease_expires_at=NULL,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND status='RUNNING' AND locked_by=:worker
            AND lease_expires_at > now() RETURNING *
        """,{"id":task_id,"tenant":self.principal.tenant_id,"worker":worker_id,"result":json.dumps(result)})
        if not item: raise PermissionError("Task lease lost or task is no longer owned")
        await self.append_audit("task.completed","task",task_id,item["product_id"],{"worker_id":worker_id})
        return item

    async def fail_task(self, task_id: str, worker_id: str, error: str, retry_delay: int, max_attempts: int):
        item=await self._one("""
          UPDATE wf_tasks SET
            status=CASE WHEN attempt_count >= LEAST(max_attempts,:max_attempts) THEN 'DEAD_LETTER' ELSE 'RETRY_SCHEDULED' END,
            next_retry_at=now()+make_interval(secs=>:delay),last_error=:error,
            locked_by=NULL,lease_expires_at=NULL,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND status='RUNNING' AND locked_by=:worker
          RETURNING *
        """,{"id":task_id,"tenant":self.principal.tenant_id,"worker":worker_id,"error":error,
             "delay":retry_delay,"max_attempts":max_attempts})
        if not item: raise PermissionError("Task lease lost")
        await self.append_audit("task."+item["status"].lower(),"task",task_id,item["product_id"],{"error":error})
        return item

    async def enqueue_task(self, *, product_id, department, assignee_role_id, title, description="",
                           priority="normal", is_sandbox=False, max_attempts=5, idempotency_key=None):
        self._product(product_id)
        return await self.create_task(product_id=product_id,department=department,assignee_role_id=assignee_role_id,
            title=title,description=description,priority=priority,idempotency_key=idempotency_key or str(uuid4()),
            is_sandbox=is_sandbox,max_attempts=max_attempts)

    async def pause_task_for_approval(self, task_id: str, action_type: str, payload: dict):
        task=await self._one("UPDATE wf_tasks SET status='AWAITING_APPROVAL',locked_by=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=:id AND tenant_id=:tenant RETURNING *",
            {"id":task_id,"tenant":self.principal.tenant_id})
        if not task: raise LookupError("Task not found")
        return await self.create_approval(product_id=task["product_id"],employee_id=task["assignee_role_id"],
            action_type=action_type,payload=payload,expires_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=15))

    async def pause_run_for_approval(self, run_id: str, action_type: str, payload: dict):
        run=await self._one("UPDATE wf_agent_runs SET status='awaiting_approval',execution_state='AWAITING_APPROVAL',updated_at=now() WHERE id=:id AND tenant_id=:tenant RETURNING *",
            {"id":run_id,"tenant":self.principal.tenant_id})
        if not run: raise LookupError("Agent run not found")
        return await self.create_approval(product_id=run["product_id"],employee_id=run["employee_id"],
            action_type=action_type,payload=payload,expires_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=15))

    async def consume_approval(self, approval_id: str, *, product_id, employee_id, action_type, payload, run_id):
        digest=hashlib.sha256(json.dumps({"tenant_id":self.principal.tenant_id,"product_id":product_id,
          "employee_id":employee_id,"action_type":action_type,"payload":payload},
          sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
        item=await self._one("""
          UPDATE wf_approvals SET consumed_at=now(),execution_run_id=:run
          WHERE id=:id AND tenant_id=:tenant AND product_id IS NOT DISTINCT FROM :product
            AND employee_id=:employee AND action_type=:action AND status='approved'
            AND approver_id IS NOT NULL AND approver_id<>requester_id
            AND expires_at>now() AND consumed_at IS NULL AND payload_hash=:hash
          RETURNING *
        """,{"id":approval_id,"tenant":self.principal.tenant_id,"product":product_id,
          "employee":employee_id,"action":action_type,"run":run_id,"hash":digest})
        if not item: raise PermissionError("Approval missing, invalid, expired, tampered or already consumed")
        await self.append_audit("approval.consumed","approval",approval_id,product_id,{"run_id":run_id})
        await self.commit_boundary()
        return item



async def repository_for(principal: Principal):
    SessionFactory = session_factory()
    async with SessionFactory() as session:
        async with PostgresWorkforceRepository(session, principal) as repository:
            yield repository


from typing import Protocol, runtime_checkable

@runtime_checkable
class WorkforceRepositoryProtocol(Protocol):
    async def create_task(self, **kwargs): ...
    async def list_tasks(self, product_id=None): ...
    async def create_agent_run(self, **kwargs): ...
    async def list_agent_runs(self, product_id=None): ...
    async def get_agent_run(self, run_id): ...
    async def write_memory(self, **kwargs): ...
    async def list_memory(self, product_id=None): ...
    async def create_approval(self, **kwargs): ...
    async def list_approvals(self, product_id=None): ...
    async def decide_approval(self, approval_id, decision, reason, product_id=None): ...
    async def validate_approval(self, approval_id, **kwargs): ...
    async def record_model_usage(self, **usage):
        self._scope(usage.get("product_id"))
        self.model_usage.append({"id":str(uuid4()),"tenant_id":self.principal.tenant_id,**usage})
        self._audit("model.usage","model_usage",usage["run_id"],usage.get("product_id"),usage)

    async def enqueue_task(self, *, product_id, department, assignee_role_id, title, description="", priority="normal", is_sandbox=False, max_attempts=5, idempotency_key=None):
        item=await self.create_task(product_id=product_id,department=department,assignee_role_id=assignee_role_id,
            title=title,description=description,priority=priority,idempotency_key=idempotency_key or str(uuid4()),
            is_sandbox=is_sandbox,max_attempts=max_attempts)
        item.setdefault("attempt_count",0); item.setdefault("max_attempts",max_attempts)
        item.setdefault("next_retry_at",datetime.now(timezone.utc)); item.setdefault("locked_by",None)
        item.setdefault("lease_expires_at",None); item.setdefault("result",None)
        return item

    async def claim_task(self, worker_id, lease_seconds, product_id=None):
        now=datetime.now(timezone.utc)
        eligible=[]
        for task in self.tasks:
            if task["tenant_id"]!=self.principal.tenant_id: continue
            if product_id is not None and task.get("product_id")!=product_id: continue
            self._scope(task.get("product_id"))
            status=task.get("status","QUEUED").upper()
            retry=task.get("next_retry_at",now)
            if isinstance(retry,str): retry=datetime.fromisoformat(retry)
            lease=task.get("lease_expires_at")
            if isinstance(lease,str): lease=datetime.fromisoformat(lease)
            if status in {"QUEUED","RETRY_SCHEDULED"} and retry<=now or status=="RUNNING" and lease and lease<=now:
                eligible.append(task)
        if not eligible: return None
        task=sorted(eligible,key=lambda x:x.get("created_at",""))[0]
        if task.get("attempt_count",0)>=task.get("max_attempts",5):
            task.update(status="DEAD_LETTER",locked_by=None,lease_expires_at=None)
            self._audit("task.dead_letter","task",task["id"],task.get("product_id"))
            return None
        task.update(status="RUNNING",locked_by=worker_id,attempt_count=task.get("attempt_count",0)+1,
            lease_expires_at=now+__import__("datetime").timedelta(seconds=lease_seconds))
        return task

    async def finish_task(self, task_id, worker_id, result):
        task=next((x for x in self.tasks if x["id"]==task_id and x["tenant_id"]==self.principal.tenant_id),None)
        if not task or task.get("status")!="RUNNING" or task.get("locked_by")!=worker_id or task["lease_expires_at"]<=datetime.now(timezone.utc):
            raise PermissionError("Task lease lost")
        task.update(status="COMPLETED",result=result,locked_by=None,lease_expires_at=None)
        self._audit("task.completed","task",task_id,task.get("product_id")); return task

    async def fail_task(self, task_id, worker_id, error, retry_delay, max_attempts):
        task=next((x for x in self.tasks if x["id"]==task_id and x["tenant_id"]==self.principal.tenant_id),None)
        if not task or task.get("locked_by")!=worker_id: raise PermissionError("Task lease lost")
        task.update(status="DEAD_LETTER" if task["attempt_count"]>=min(task["max_attempts"],max_attempts) else "RETRY_SCHEDULED",
            last_error=error,locked_by=None,lease_expires_at=None,
            next_retry_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=retry_delay))
        self._audit("task."+task["status"].lower(),"task",task_id,task.get("product_id")); return task

    async def pause_task_for_approval(self, task_id, action_type, payload):
        task=next((x for x in self.tasks if x["id"]==task_id and x["tenant_id"]==self.principal.tenant_id),None)
        if not task: raise LookupError("Task not found")
        task.update(status="AWAITING_APPROVAL",locked_by=None,lease_expires_at=None)
        return await self.create_approval(product_id=task.get("product_id"),employee_id=task["assignee_role_id"],
            action_type=action_type,payload=payload,expires_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=15))

    async def pause_run_for_approval(self, run_id, action_type, payload):
        run=next((x for x in self.runs if x["id"]==run_id and x["tenant_id"]==self.principal.tenant_id),None)
        if not run: raise LookupError("Agent run not found")
        run["status"]="awaiting_approval"
        return await self.create_approval(product_id=run.get("product_id"),employee_id=run["employee_id"],
            action_type=action_type,payload=payload,expires_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=15))

    async def consume_approval(self, approval_id, *, product_id, employee_id, action_type, payload, run_id):
        item=await self.validate_approval(approval_id,product_id=product_id,employee_id=employee_id,action_type=action_type,payload=payload)
        if item.get("consumed_at") is not None: raise PermissionError("Approval already consumed")
        item["consumed_at"]=datetime.now(timezone.utc); item["execution_run_id"]=run_id
        self._audit("approval.consumed","approval",approval_id,product_id,{"run_id":run_id})
        return item

    async def append_audit(self, action, resource_type, resource_id, product_id, details): ...
    async def list_audit(self, product_id=None): ...
    async def record_model_usage(self, **usage): ...
    async def commit_boundary(self): ...
    async def resume_agent_run(self, run_id, approval_id): ...
    async def claim_task(self, worker_id, lease_seconds, product_id=None): ...
    async def finish_task(self, task_id, worker_id, result): ...
    async def fail_task(self, task_id, worker_id, error, retry_delay, max_attempts): ...
    async def enqueue_task(self, **kwargs): ...
    async def pause_run_for_approval(self, run_id, action_type, payload): ...
    async def pause_task_for_approval(self, task_id, action_type, payload): ...
    async def consume_approval(self, approval_id, **kwargs): ...


class InMemoryWorkforceRepository:
    """Explicitly injected, per-app test adapter. Never selected by production."""
    def __init__(self, principal: Principal, products: set[str]):
        self.principal, self.products = principal, products
        self.tasks, self.runs, self.memory, self.approvals, self.audit, self.model_usage = [], [], [], [], []

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
        item.setdefault("attempt_count",0); item.setdefault("max_attempts",int(kw.get("max_attempts",5))); item.setdefault("is_sandbox",bool(kw.get("is_sandbox",False)))
        item["status"]="QUEUED"; self.tasks.append(item); self._audit("task.create","task",item["id"],product); return item

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
        item.setdefault("is_sandbox",bool(kw.get("is_sandbox",False))); item.setdefault("execution_state","QUEUED")
        self.runs.append(item); self._audit("agent.run","agent_run",item["id"],kw["product_id"]); return item

    async def get_agent_run(self, run_id):
        return next((x for x in self.runs if x["id"]==run_id and x["tenant_id"]==self.principal.tenant_id),None)

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

    async def decide_approval(self, approval_id, decision, reason, product_id=None):
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
        if item["status"]!="approved" or item["expires_at"]<=datetime.now(timezone.utc) or item["approver_id"]==item["requester_id"] or item.get("consumed_at") is not None:
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



# Backwards-compatible internal alias; production dependencies use the explicit name.
TenantRepository = PostgresWorkforceRepository
