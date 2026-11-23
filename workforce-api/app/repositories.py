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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .policy import Principal


@lru_cache(maxsize=1)
def session_factory():
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for Workforce API runtime")
    parts = urlsplit(settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1))
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"sslmode", "channel_binding"}]
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    connect_args = {"ssl": "require"} if "sslmode=" in settings.database_url else {}
    engine = create_async_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


from .reporting_repository import ReportingRepositoryMixin
from .release_repository import ReleaseRepositoryMixin

class PostgresWorkforceRepository(ReleaseRepositoryMixin, ReportingRepositoryMixin):
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
        await self.session.execute(
            text("SELECT set_config('app.can_decide_approvals', :allowed, true)"),
            {"allowed": "true" if "*" in self.principal.permissions or "approval.decide" in self.principal.permissions else "false"},
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
          ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
          RETURNING *
        """, {"id": str(uuid4()), "tenant": self.principal.tenant_id, "product": product_id,
              "department": department, "assignee": assignee_role_id, "title": title,
              "description": description, "priority": priority, "actor": self.principal.user_id,
              "idem": idempotency_key, "sandbox": is_sandbox, "max_attempts": max_attempts})
        if not item:
            item=await self._one("SELECT * FROM wf_tasks WHERE tenant_id=:tenant AND idempotency_key=:idem",
                {"tenant":self.principal.tenant_id,"idem":idempotency_key})
            if not item: raise RuntimeError("Idempotent task insert conflict could not be resolved")
            expected={"product_id":product_id,"department":department,"assignee_role_id":assignee_role_id,
                "title":title,"description":description,"priority":priority,"is_sandbox":is_sandbox}
            if any(item.get(key)!=value for key,value in expected.items()):
                raise ValueError("Idempotency key was already used for a different task payload")
            return item
        await self.append_audit("task.create", "task", str(item["id"]), product_id, {"idempotency_key": idempotency_key})
        return item

    async def list_tasks(self, product_id: str | None = None):
        self._product(product_id)
        return await self._rows("""
          SELECT * FROM wf_tasks
          WHERE tenant_id=:tenant AND (CAST(:product AS text) IS NULL OR product_id=:product)
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
        expected=expected_status.upper(); target=new_status.upper()
        transitions={"QUEUED":{"RUNNING","AWAITING_APPROVAL","CANCELLED"},
            "RUNNING":{"RUNNING","AWAITING_APPROVAL","COMPLETED","RETRY_SCHEDULED","DEAD_LETTER","CANCELLED"},
            "RETRY_SCHEDULED":{"QUEUED","RUNNING","DEAD_LETTER","CANCELLED"},
            "AWAITING_APPROVAL":{"QUEUED","RUNNING","CANCELLED"}}
        if target not in transitions.get(expected,set()): raise ValueError("Invalid agent-run state transition")
        item=await self._one("""
          UPDATE wf_agent_runs SET execution_state=:target,status=:status,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND execution_state=:expected RETURNING *
        """,{"id":run_id,"tenant":self.principal.tenant_id,"expected":expected,
             "target":target,"status":target.lower()})
        if not item: raise PermissionError("Agent run state changed or run is unavailable")
        await self.append_audit("agent_run.state_changed","agent_run",run_id,item["product_id"],
            {"from":expected,"to":target})
        return item

    async def get_agent_run(self, run_id: str):
        return await self._one("SELECT * FROM wf_agent_runs WHERE id=:id AND tenant_id=:tenant",
            {"id":run_id,"tenant":self.principal.tenant_id})

    async def list_agent_runs(self, product_id: str | None = None):
        self._product(product_id)
        if product_id is None:
            return await self._rows(
                "SELECT * FROM wf_agent_runs WHERE tenant_id=:tenant ORDER BY created_at DESC",
                {"tenant": self.principal.tenant_id},
            )
        return await self._rows(
            "SELECT * FROM wf_agent_runs WHERE tenant_id=:tenant AND product_id=:product ORDER BY created_at DESC",
            {"tenant": self.principal.tenant_id, "product": product_id},
        )

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
          FROM wf_memory WHERE tenant_id=:tenant AND (CAST(:product AS text) IS NULL OR product_id=CAST(:product AS text))
          ORDER BY created_at DESC
        """, {"tenant": self.principal.tenant_id, "product": product_id})

    async def create_approval(self, *, product_id: str | None, employee_id: str, action_type: str,
                              payload: dict, expires_at: datetime):
        self._product(product_id)
        if expires_at <= datetime.now(timezone.utc): raise ValueError("Approval expiry must be in the future")
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
                  :requester,'PENDING',:expires,now()) RETURNING *
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
            AND status='PENDING' AND requester_id <> :approver AND expires_at > now()
          RETURNING *
        """, {"decision": decision.upper(), "approver": self.principal.user_id, "reason": reason,
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
            AND action_type=:action AND status='APPROVED' AND expires_at > now() AND consumed_at IS NULL
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
          AND (CAST(:product AS text) IS NULL OR product_id=CAST(:product AS text)) ORDER BY created_at DESC
        """, {"tenant": self.principal.tenant_id, "product": product_id})

    async def list_audit(self, product_id: str | None = None):
        self._product(product_id)
        return await self._rows("""
          SELECT * FROM wf_audit_events WHERE tenant_id=:tenant
          AND (CAST(:product AS text) IS NULL OR product_id=CAST(:product AS text)) ORDER BY occurred_at DESC
        """, {"tenant": self.principal.tenant_id, "product": product_id})

    async def append_audit(self, action: str, resource_type: str, resource_id: str,
                           product_id: str | None, details: dict):
        await self.session.execute(text("""
          INSERT INTO wf_audit_events
            (id,tenant_id,product_id,actor_id,action,resource_type,resource_id,details,occurred_at)
          VALUES (:id,:tenant,:product,:actor,:action,:type,:resource,CAST(:details AS jsonb),now())
        """), {"id": str(uuid4()), "tenant": self.principal.tenant_id, "product": product_id,
               "actor": self.principal.user_id, "action": action, "type": resource_type,
               "resource": resource_id, "details": json.dumps(details, default=str)})

    async def upsert_prospect(self, *, product_id, dedupe_key, business_name, city, segment,
                             decision_maker_name, public_contact_channel, source_url, evidence_urls,
                             evidence_snippet, confidence_score, icp_fit_tier, is_synthetic, metadata):
        self._product(product_id)
        if not 0 <= float(confidence_score) <= 1:
            raise ValueError("confidence_score must be between 0 and 1")
        return await self._one("""
          INSERT INTO prospect_records
            (id,tenant_id,product_id,dedupe_key,business_name,city,segment,decision_maker_name,
             public_contact_channel,source_url,evidence_urls,evidence_snippet,confidence_score,
             icp_fit_tier,is_synthetic,metadata)
          VALUES (:id,:tenant,:product,:dedupe,:name,:city,:segment,:decision_maker,
             :contact,:source,CAST(:evidence_urls AS jsonb),:snippet,:confidence,:tier,:synthetic,
             CAST(:metadata AS jsonb))
          ON CONFLICT (tenant_id,product_id,dedupe_key) DO UPDATE SET
            evidence_urls=(
              SELECT COALESCE(jsonb_agg(DISTINCT value),'[]'::jsonb)
              FROM jsonb_array_elements(prospect_records.evidence_urls || EXCLUDED.evidence_urls) AS value
            ),
            source_url=EXCLUDED.source_url,
            evidence_snippet=CASE WHEN length(EXCLUDED.evidence_snippet)>length(prospect_records.evidence_snippet)
              THEN EXCLUDED.evidence_snippet ELSE prospect_records.evidence_snippet END,
            decision_maker_name=COALESCE(EXCLUDED.decision_maker_name,prospect_records.decision_maker_name),
            public_contact_channel=COALESCE(EXCLUDED.public_contact_channel,prospect_records.public_contact_channel),
            confidence_score=GREATEST(prospect_records.confidence_score,EXCLUDED.confidence_score),
            icp_fit_tier=CASE
              WHEN EXCLUDED.icp_fit_tier='HIGH' OR prospect_records.icp_fit_tier='HIGH' THEN 'HIGH'
              WHEN EXCLUDED.icp_fit_tier='MEDIUM' OR prospect_records.icp_fit_tier='MEDIUM' THEN 'MEDIUM'
              ELSE EXCLUDED.icp_fit_tier END,
            is_synthetic=prospect_records.is_synthetic AND EXCLUDED.is_synthetic,
            metadata=prospect_records.metadata || EXCLUDED.metadata,
            last_evidenced_at=now(),updated_at=now()
          RETURNING *
        """, {"id":str(uuid4()),"tenant":self.principal.tenant_id,"product":product_id,
              "dedupe":dedupe_key,"name":business_name,"city":city,"segment":segment,
              "decision_maker":decision_maker_name,"contact":public_contact_channel,
              "source":source_url,"evidence_urls":json.dumps(sorted(set(evidence_urls))),
              "snippet":evidence_snippet,"confidence":confidence_score,"tier":icp_fit_tier,
              "synthetic":is_synthetic,"metadata":json.dumps(metadata or {})})

    async def list_prospects(self, product_id=None, limit=200):
        self._product(product_id)
        if product_id is None:
            return await self._rows("SELECT * FROM prospect_records WHERE tenant_id=:tenant ORDER BY discovered_at DESC LIMIT :limit",
                {"tenant":self.principal.tenant_id,"limit":limit})
        return await self._rows("SELECT * FROM prospect_records WHERE tenant_id=:tenant AND product_id=:product ORDER BY discovered_at DESC LIMIT :limit",
            {"tenant":self.principal.tenant_id,"product":product_id,"limit":limit})

    async def create_campaign(self, *, product_id, name, target_icp_segment, channel,
                              messaging_brief, ai_identity_disclosure, is_sandbox, created_by, metadata):
        self._product(product_id)
        return await self._one("""
          INSERT INTO campaign_plans
            (tenant_id,product_id,name,target_icp_segment,channel,messaging_brief,
             ai_identity_disclosure,is_sandbox,created_by,metadata)
          VALUES (:tenant,:product,:name,:segment,:channel,:brief,:disclosure,:sandbox,:actor,
                  CAST(:metadata AS jsonb)) RETURNING *
        """, {"tenant":self.principal.tenant_id,"product":product_id,"name":name,
              "segment":target_icp_segment,"channel":channel,"brief":messaging_brief,
              "disclosure":ai_identity_disclosure,"sandbox":is_sandbox,"actor":created_by,
              "metadata":json.dumps(metadata or {})})

    async def get_campaign(self, campaign_id):
        return await self._one("SELECT * FROM campaign_plans WHERE id=:id AND tenant_id=:tenant",
            {"id":campaign_id,"tenant":self.principal.tenant_id})

    async def list_campaigns(self, product_id=None, limit=200):
        self._product(product_id)
        if product_id is None:
            return await self._rows("SELECT * FROM campaign_plans WHERE tenant_id=:tenant ORDER BY created_at DESC LIMIT :limit",
                {"tenant":self.principal.tenant_id,"limit":limit})
        return await self._rows("SELECT * FROM campaign_plans WHERE tenant_id=:tenant AND product_id=:product ORDER BY created_at DESC LIMIT :limit",
            {"tenant":self.principal.tenant_id,"product":product_id,"limit":limit})

    async def request_campaign_approval(self, *, campaign_id, product_id, employee_id, messaging_brief):
        campaign=await self.get_campaign(campaign_id)
        if not campaign or campaign["product_id"]!=product_id:
            raise LookupError("Campaign not found in this tenant/product scope")
        if campaign["status"] not in {"DRAFT","PAUSED"} or campaign["is_sandbox"]:
            raise PermissionError("Only a live draft campaign can request activation approval")
        approval=await self.create_approval(product_id=product_id,employee_id=employee_id,
            action_type="campaign.activate",
            payload={"campaign_id":str(campaign_id
