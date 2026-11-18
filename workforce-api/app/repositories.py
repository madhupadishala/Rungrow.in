"""Tenant-scoped async PostgreSQL repositories for RunGrow Workforce.

All methods execute inside a transaction that installs verified Principal scope
with SET LOCAL. No repository method accepts tenant_id from request data.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .policy import Principal

engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


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
        return await self._one("""
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
        return await self._one("""
          INSERT INTO wf_agent_runs
            (id, tenant_id, product_id, employee_id, requested_by, task, status, summary,
             steps, correlation_id, created_at)
          VALUES (:id,:tenant,:product,:employee,:actor,:task,:status,:summary,
                  CAST(:steps AS jsonb),:correlation,now()) RETURNING *
        """, {"id": str(uuid4()), "tenant": self.principal.tenant_id, "product": product_id,
              "employee": employee_id, "actor": self.principal.user_id, "task": task,
              "status": status, "summary": summary, "steps": json.dumps(steps),
              "correlation": correlation_id})

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
        return await self._one("""
          INSERT INTO wf_memory
            (id, tenant_id, product_id, role_id, department, content, source_ref,
             retention_days, embedding, created_at)
          VALUES (:id,:tenant,:product,:role,:department,:content,:source,:retention,
                  CAST(:embedding AS vector),now()) RETURNING id, tenant_id, product_id, role_id, department, content, source_ref, retention_days, created_at
        """, {"id": str(uuid4()), "tenant": self.principal.tenant_id, "product": product_id,
              "role": role_id, "department": department, "content": content,
              "source": source_ref, "retention": retention_days, "embedding": vector})

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

    async def decide_approval(self, approval_id: str, decision: str, reason: str):
        if decision not in {"approved", "rejected"}:
            raise ValueError("Invalid approval decision")
        item = await self._one("""
          UPDATE wf_approvals SET status=:decision, approver_id=:approver,
                 decision_reason=:reason, decided_at=now()
          WHERE id=:id AND tenant_id=:tenant AND status='pending'
            AND requester_id <> :approver AND expires_at > now()
          RETURNING *
        """, {"decision": decision, "approver": self.principal.user_id, "reason": reason,
              "id": approval_id, "tenant": self.principal.tenant_id})
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
    async with SessionFactory() as session:
        async with TenantRepository(session, principal) as repository:
            yield repository
