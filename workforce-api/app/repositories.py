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
            payload={"campaign_id":str(campaign_id),"messaging_brief":messaging_brief},
            expires_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(hours=24))
        await self._one("UPDATE campaign_plans SET status='AWAITING_APPROVAL' WHERE id=:id AND tenant_id=:tenant RETURNING id",
            {"id":campaign_id,"tenant":self.principal.tenant_id})
        return approval

    async def transition_campaign(self, campaign_id, *, status, approval_id, execution_run_id):
        if status not in {"APPROVED","ACTIVE","PAUSED"}:
            raise ValueError("Unsupported campaign transition")
        item=await self._one("""
          UPDATE campaign_plans SET status=:status,approval_id=:approval,
            execution_run_id=:run,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant RETURNING *
        """, {"status":status,"approval":approval_id,"run":execution_run_id,
              "id":campaign_id,"tenant":self.principal.tenant_id})
        if not item:
            raise LookupError("Campaign not found")
        await self.append_audit("campaign."+status.lower(),"campaign",str(campaign_id),
            item["product_id"],{"approval_id":str(approval_id),"execution_run_id":str(execution_run_id)})
        return item

    async def start_agent_run(self, run_id):
        item=await self._one("""
          UPDATE wf_agent_runs SET execution_state='RUNNING',status='running',updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND execution_state IN ('QUEUED','AWAITING_APPROVAL')
          RETURNING *
        """,{"id":run_id,"tenant":self.principal.tenant_id})
        if not item: raise PermissionError("Agent run is not in an executable state")
        await self.append_audit("agent_run.started","agent_run",run_id,item["product_id"],{})
        return item

    async def complete_agent_run(self, run_id, result):
        item=await self._one("""
          UPDATE wf_agent_runs SET execution_state='COMPLETED',status='completed',
            summary=COALESCE(:summary,summary),updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND execution_state='RUNNING' RETURNING *
        """,{"id":run_id,"tenant":self.principal.tenant_id,"summary":result.get("summary") if isinstance(result,dict) else None})
        if not item: raise PermissionError("Agent run is not running")
        await self.append_audit("agent_run.completed","agent_run",run_id,item["product_id"],{})
        return item

    async def fail_agent_run(self, run_id, error):
        item=await self._one("""
          UPDATE wf_agent_runs SET execution_state='DEAD_LETTER',status='failed',
            summary=:error,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND execution_state='RUNNING' RETURNING *
        """,{"id":run_id,"tenant":self.principal.tenant_id,"error":error[:4000]})
        if not item: raise PermissionError("Agent run is not running")
        await self.append_audit("agent_run.failed","agent_run",run_id,item["product_id"],{"error":error[:1000]})
        return item

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
              AND a.status='APPROVED' AND a.expires_at>now() AND a.consumed_at IS NULL)
          RETURNING r.*
        """,{"run":run_id,"tenant":self.principal.tenant_id,"approval":approval_id})
        if not item: raise PermissionError("Run cannot resume without matching live approval")
        await self.append_audit("agent_run.resumed","agent_run",run_id,item["product_id"],{"approval_id":approval_id})
        return item

    async def claim_adapter_idempotency(self, *, tenant_id, external_tenant_id, product_id, direction, idempotency_key, payload_hash, correlation_id):
        if tenant_id != self.principal.tenant_id: raise PermissionError("Adapter tenant mismatch")
        self._product(product_id)
        item=await self._one("""INSERT INTO adapter_idempotency_keys
          (id,tenant_id,external_tenant_id,product_id,direction,idempotency_key,payload_hash,status,correlation_id,created_at)
          VALUES(:id,:tenant,:external_tenant,:product,:direction,:key,:hash,'PROCESSING',:correlation,now())
          ON CONFLICT(tenant_id,external_tenant_id,product_id,direction,idempotency_key) DO NOTHING RETURNING *""",
          {"id":str(uuid4()),"tenant":tenant_id,"external_tenant":external_tenant_id,"product":product_id,"direction":direction,
           "key":idempotency_key,"hash":payload_hash,"correlation":correlation_id})
        if item:
            await self.append_audit("adapter.idempotency.claimed","adapter_idempotency",idempotency_key,
                product_id,{"direction":direction,"external_tenant_id":external_tenant_id,
                "correlation_id":correlation_id})
            return {"created":True,"response":None}
        item=await self._one("""SELECT * FROM adapter_idempotency_keys WHERE tenant_id=:tenant AND external_tenant_id=:external_tenant
          AND product_id=:product AND direction=:direction AND idempotency_key=:key""",
          {"tenant":tenant_id,"external_tenant":external_tenant_id,"product":product_id,"direction":direction,"key":idempotency_key})
        if not item: raise RuntimeError("Idempotency conflict row unavailable")
        if item["payload_hash"] != payload_hash: raise ValueError("Idempotency-Key reused with a different payload")
        await self.append_audit("adapter.idempotency.replayed","adapter_idempotency",idempotency_key,
            product_id,{"direction":direction,"external_tenant_id":external_tenant_id,
            "correlation_id":correlation_id})
        return {"created":False,"response":item.get("response_json"),"status":item["status"]}

    async def complete_adapter_idempotency(self, *, tenant_id, external_tenant_id, product_id, direction, idempotency_key, response):
        if tenant_id != self.principal.tenant_id: raise PermissionError("Adapter tenant mismatch")
        item=await self._one("""UPDATE adapter_idempotency_keys SET status='COMPLETED',
          response_json=CAST(:response AS jsonb),completed_at=now()
          WHERE tenant_id=:tenant AND external_tenant_id=:external_tenant AND product_id=:product AND direction=:direction
          AND idempotency_key=:key AND status='PROCESSING' RETURNING id""",
          {"tenant":tenant_id,"external_tenant":external_tenant_id,"product":product_id,"direction":direction,"key":idempotency_key,
           "response":json.dumps(response)})
        if not item: raise PermissionError("Idempotency record unavailable or completed")
        await self.append_audit("adapter.idempotency.completed","adapter_idempotency",idempotency_key,
            product_id,{"direction":direction,"external_tenant_id":external_tenant_id})

    async def record_product_telemetry(self, *, tenant_id, external_tenant_id, product_id, event, correlation_id):
        if tenant_id != self.principal.tenant_id: raise PermissionError("Telemetry tenant mismatch")
        self._product(product_id)
        item=await self._one("""INSERT INTO product_telemetry_events
          (id,tenant_id,external_tenant_id,product_id,event_id,event_type,recognised_revenue_inr,occurred_at,
           source_ref,metadata,correlation_id,received_at)
          VALUES(:id,:tenant,:external_tenant,:product,:event_id,:event_type,:revenue,:occurred,:source,
           CAST(:metadata AS jsonb),:correlation,now())
          ON CONFLICT(tenant_id,external_tenant_id,product_id,event_id) DO NOTHING RETURNING id""",
          {"id":str(uuid4()),"tenant":tenant_id,"external_tenant":external_tenant_id,"product":product_id,"event_id":event["event_id"],
           "event_type":event["event_type"],"revenue":event["recognised_revenue_inr"],
           "occurred":event.get("occurred_at"),"source":event.get("source_ref"),
           "metadata":json.dumps(event.get("metadata") or {}),"correlation":correlation_id})
        if item:
            await self.append_audit("product.telemetry.received","product_telemetry",event["event_id"],
              product_id,{"event_type":event["event_type"],"recognised_revenue_inr":str(event["recognised_revenue_inr"]),
              "correlation_id":correlation_id})
            return True
        return False

    async def record_model_usage(self, **usage):
        await self.session.execute(text("""
          INSERT INTO wf_model_usage(id,tenant_id,product_id,employee_id,run_id,is_sandbox,provider,model,
            prompt_tokens,completion_tokens,estimated_cost_inr,latency_ms,created_at)
          VALUES(:id,:tenant,:product,:employee,:run,:sandbox,:provider,:model,:prompt,:completion,:cost,:latency,now())
        """),{"id":str(uuid4()),"tenant":self.principal.tenant_id,"product":usage["product_id"],
          "employee":usage["employee_id"],"run":usage["run_id"],"sandbox":bool(usage.get("is_sandbox",False)),"provider":usage["provider"],
          "model":usage["model"],"prompt":usage["prompt_tokens"],"completion":usage["completion_tokens"],
          "cost":usage["estimated_cost_inr"],"latency":usage["latency_ms"]})
        await self.append_audit("model.usage","model_usage",usage["run_id"],usage["product_id"],usage)

    async def resume_task(self, task_id, approval_id):
        item=await self._one("""
          UPDATE wf_tasks t SET status='QUEUED',next_retry_at=now(),updated_at=now()
          WHERE t.id=:task AND t.tenant_id=:tenant AND t.status='AWAITING_APPROVAL'
            AND EXISTS (SELECT 1 FROM wf_approvals a WHERE a.id=:approval AND a.tenant_id=t.tenant_id
              AND a.product_id IS NOT DISTINCT FROM t.product_id AND a.employee_id=t.assignee_role_id
              AND a.status='APPROVED' AND a.expires_at>now() AND a.consumed_at IS NULL)
          RETURNING t.*
        """,{"task":task_id,"tenant":self.principal.tenant_id,"approval":approval_id})
        if not item: raise PermissionError("Task cannot resume without a matching live approval")
        await self.append_audit("task.resumed","task",task_id,item["product_id"],{"approval_id":approval_id})
        return item

    async def claim_task(self, worker_id: str, lease_seconds: int, product_id: str | None=None):
        params={"tenant":self.principal.tenant_id,"products":",".join(self.principal.product_ids),
            "admin":"true" if "*" in self.principal.permissions else "false","requested_product":product_id}
        exhausted=await self.session.execute(text("""
          UPDATE wf_tasks SET status='DEAD_LETTER',locked_by=NULL,lease_expires_at=NULL,updated_at=now()
          WHERE tenant_id=:tenant AND attempt_count>=max_attempts
            AND ((status IN ('QUEUED','RETRY_SCHEDULED') AND next_retry_at<=now())
              OR (status='RUNNING' AND lease_expires_at<=now()))
            AND (:requested_product IS NULL OR product_id=:requested_product)
            AND (product_id IS NULL OR product_id=ANY(string_to_array(:products,',')) OR :admin='true')
          RETURNING id,product_id
        """),params)
        for row in exhausted.mappings().all():
            await self.append_audit("task.dead_letter","task",str(row["id"]),row["product_id"],{"reason":"max_attempts_exhausted"})
        item=await self._one("""
          WITH candidate AS (
            SELECT id FROM wf_tasks
            WHERE ((status IN ('QUEUED','RETRY_SCHEDULED') AND next_retry_at<=now())
              OR (status='RUNNING' AND lease_expires_at<=now()))
              AND attempt_count<max_attempts AND tenant_id=:tenant
              AND (:requested_product IS NULL OR product_id=:requested_product)
              AND (product_id IS NULL OR product_id=ANY(string_to_array(:products,',')) OR :admin='true')
            ORDER BY next_retry_at,created_at
            FOR UPDATE SKIP LOCKED LIMIT 1
          )
          UPDATE wf_tasks t SET status='RUNNING',locked_by=:worker,
            lease_expires_at=now()+make_interval(secs=>:lease),
            attempt_count=t.attempt_count+1,updated_at=now()
          FROM candidate c WHERE t.id=c.id AND t.tenant_id=:tenant
          RETURNING t.*
        """,{**params,"worker":worker_id,"lease":lease_seconds})
        if item:
            await self.append_audit("task.claimed","task",str(item["id"]),item["product_id"],{"worker_id":worker_id})
        return item

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
        task=await self._one("UPDATE wf_tasks SET status='AWAITING_APPROVAL',locked_by=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=:id AND tenant_id=:tenant AND status='RUNNING' RETURNING *",
            {"id":task_id,"tenant":self.principal.tenant_id})
        if not task: raise LookupError("Task not found or not RUNNING")
        return await self.create_approval(product_id=task["product_id"],employee_id=task["assignee_role_id"],
            action_type=action_type,payload=payload,expires_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=15))

    async def pause_run_for_approval(self, run_id: str, action_type: str, payload: dict):
        run=await self._one("UPDATE wf_agent_runs SET status='awaiting_approval',execution_state='AWAITING_APPROVAL',updated_at=now() WHERE id=:id AND tenant_id=:tenant AND execution_state IN ('QUEUED','RUNNING') RETURNING *",
            {"id":run_id,"tenant":self.principal.tenant_id})
        if not run: raise LookupError("Agent run not found or not resumable")
        return await self.create_approval(product_id=run["product_id"],employee_id=run["employee_id"],
            action_type=action_type,payload=payload,expires_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=15))

    async def consume_approval(self, approval_id: str, *, product_id, employee_id, action_type, payload, run_id):
        digest=hashlib.sha256(json.dumps({"tenant_id":self.principal.tenant_id,"product_id":product_id,
          "employee_id":employee_id,"action_type":action_type,"payload":payload},
          sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
        item=await self._one("""
          UPDATE wf_approvals SET consumed_at=now(),execution_run_id=:run
          WHERE id=:id AND tenant_id=:tenant AND product_id IS NOT DISTINCT FROM :product
            AND employee_id=:employee AND action_type=:action AND status='APPROVED'
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
    async def get_agent_run(self, run_id): ...
    async def transition_agent_run(self, run_id, expected_status, new_status): ...
    async def list_agent_runs(self, product_id=None): ...
    async def write_memory(self, **kwargs): ...
    async def list_memory(self, product_id=None): ...
    async def create_approval(self, **kwargs): ...
    async def list_approvals(self, product_id=None): ...
    async def decide_approval(self, approval_id, decision, reason, product_id=None): ...
    async def validate_approval(self, approval_id, **kwargs): ...
    async def append_audit(self, action, resource_type, resource_id, product_id, details): ...
    async def list_audit(self, product_id=None): ...
    async def upsert_prospect(self, **kwargs): ...
    async def list_prospects(self, product_id=None, limit=200): ...
    async def create_campaign(self, **kwargs): ...
    async def get_campaign(self, campaign_id): ...
    async def list_campaigns(self, product_id=None, limit=200): ...
    async def request_campaign_approval(self, **kwargs): ...
    async def transition_campaign(self, campaign_id, **kwargs): ...
    async def claim_adapter_idempotency(self, **kwargs): ...
    async def complete_adapter_idempotency(self, **kwargs): ...
    async def record_product_telemetry(self, **kwargs): ...
    async def record_model_usage(self, **usage): ...
    async def commit_boundary(self): ...
    async def start_agent_run(self, run_id): ...
    async def complete_agent_run(self, run_id, result): ...
    async def fail_agent_run(self, run_id, error): ...
    async def resume_agent_run(self, run_id, approval_id): ...
    async def claim_task(self, worker_id, lease_seconds, product_id=None): ...
    async def resume_task(self, task_id, approval_id): ...
    async def finish_task(self, task_id, worker_id, result): ...
    async def fail_task(self, task_id, worker_id, error, retry_delay, max_attempts): ...
    async def enqueue_task(self, **kwargs): ...
    async def pause_run_for_approval(self, run_id, action_type, payload): ...
    async def pause_task_for_approval(self, task_id, action_type, payload): ...
    async def consume_approval(self, approval_id, **kwargs): ...


class InMemoryWorkforceRepository(ReleaseRepositoryMixin, ReportingRepositoryMixin):
    """Explicitly injected, per-app test adapter. Never selected by production."""
    def __init__(self, principal: Principal, products: set[str]):
        self.principal, self.products = principal, products
        self.tasks, self.runs, self.memory, self.approvals, self.audit, self.model_usage = [], [], [], [], [], []
        self.adapter_idempotency, self.product_telemetry = [], []
        self.investment_capital_events, self.ceo_briefings = [], []

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
        old=next((x for x in self.tasks if x["idempotency_key"]==key and x["tenant_id"]==self.principal.tenant_id),None)
        if old:
            expected={"product_id":product,"department":kw["department"],"assignee_role_id":kw["assignee_role_id"],
                "title":kw["title"],"description":kw["description"],"priority":kw["priority"],
                "is_sandbox":bool(kw.get("is_sandbox",False))}
            if any(old.get(field)!=value for field,value in expected.items()):
                raise ValueError("Idempotency key was already used for a different task payload")
            return old
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

    async def transition_agent_run(self, run_id, expected_status, new_status):
        run=await self.get_agent_run(run_id)
        expected=expected_status.upper(); target=new_status.upper()
        transitions={"QUEUED":{"RUNNING","AWAITING_APPROVAL","CANCELLED"},
            "RUNNING":{"RUNNING","AWAITING_APPROVAL","COMPLETED","RETRY_SCHEDULED","DEAD_LETTER","CANCELLED"},
            "RETRY_SCHEDULED":{"QUEUED","RUNNING","DEAD_LETTER","CANCELLED"},
            "AWAITING_APPROVAL":{"QUEUED","RUNNING","CANCELLED"}}
        if target not in transitions.get(expected,set()): raise ValueError("Invalid agent-run state transition")
        if not run or run.get("execution_state")!=expected: raise PermissionError("Agent run state changed or run is unavailable")
        run.update(execution_state=target,status=target.lower())
        self._audit("agent_run.state_changed","agent_run",run_id,run.get("product_id"),{"from":expected,"to":target})
        return run

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
        if kw["expires_at"] <= datetime.now(timezone.utc): raise ValueError("Approval expiry must be in the future")
        digest=hashlib.sha256(json.dumps({"tenant_id":self.principal.tenant_id,"product_id":kw["product_id"],
          "employee_id":kw["employee_id"],"action_type":kw["action_type"],"payload":kw["payload"]},
          sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
        item={"id":str(uuid4()),"tenant_id":self.principal.tenant_id,"requester_id":self.principal.user_id,
          "approver_id":None,"status":"PENDING","payload_hash":digest,"created_at":datetime.now(timezone.utc).isoformat(),**kw}
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
        if item["status"]!="PENDING" or item["expires_at"]<=datetime.now(timezone.utc): raise PermissionError("Approval expired or already decided")
        item.update(status=decision.upper(),approver_id=self.principal.user_id,decision_reason=reason,decided_at=datetime.now(timezone.utc))
        self._audit("approval."+decision,"approval",approval_id,item["product_id"]); return item

    async def validate_approval(self, approval_id, **kw):
        item=next((x for x in self.approvals if x["id"]==approval_id and x["tenant_id"]==self.principal.tenant_id),None)
        if not item: raise LookupError("Approval not found")
        if item["status"]!="APPROVED" or item["expires_at"]<=datetime.now(timezone.utc) or item["approver_id"]==item["requester_id"] or item.get("consumed_at") is not None:
            raise PermissionError("Approval validation failed")
        for k in ("product_id","employee_id","action_type"):
            if item[k]!=kw[k]: raise PermissionError("Approval binding mismatch")
        expected=hashlib.sha256(json.dumps({"tenant_id":self.principal.tenant_id,"product_id":kw["product_id"],
          "employee_id":kw["employee_id"],"action_type":kw["action_type"],"payload":kw["payload"]},
          sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
        if not __import__("hmac").compare_digest(item["payload_hash"],expected): raise PermissionError("Payload integrity failure")
        return item

    async def resume_task(self, task_id, approval_id):
        task=next((x for x in self.tasks if x["id"]==task_id and x["tenant_id"]==self.principal.tenant_id),None)
        approval=next((x for x in self.approvals if x["id"]==approval_id and x["tenant_id"]==self.principal.tenant_id),None)
        if task: self._scope(task.get("product_id"))
        if not task or task.get("status")!="AWAITING_APPROVAL" or not approval or approval["status"]!="APPROVED" or approval["expires_at"]<=datetime.now(timezone.utc) or approval["product_id"]!=task.get("product_id") or approval["employee_id"]!=task["assignee_role_id"] or approval.get("consumed_at"):
            raise PermissionError("Task cannot resume without matching live approval")
        task.update(status="QUEUED",next_retry_at=datetime.now(timezone.utc))
        self._audit("task.resumed","task",task_id,task.get("product_id"),{"approval_id":approval_id})
        return task

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
            if (status in {"QUEUED","RETRY_SCHEDULED"} and retry<=now) or (status=="RUNNING" and lease and lease<=now):
                eligible.append(task)
        if not eligible: return None
        task=sorted(eligible,key=lambda x:x.get("created_at",""))[0]
        if task.get("attempt_count",0)>=task.get("max_attempts",5):
            task.update(status="DEAD_LETTER",locked_by=None,lease_expires_at=None)
            self._audit("task.dead_letter","task",task["id"],task.get("product_id"))
            return None
        task.update(status="RUNNING",locked_by=worker_id,attempt_count=task.get("attempt_count",0)+1,
            lease_expires_at=now+__import__("datetime").timedelta(seconds=lease_seconds))
        self._audit("task.claimed","task",task["id"],task.get("product_id"),{"worker_id":worker_id})
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
        terminal=task["attempt_count"]>=min(task["max_attempts"],max_attempts)
        task.update(status="DEAD_LETTER" if terminal else "RETRY_SCHEDULED",last_error=error,
            locked_by=None,lease_expires_at=None,
            next_retry_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(seconds=retry_delay))
        self._audit("task."+task["status"].lower(),"task",task_id,task.get("product_id"),{"error":error})
        return task

    async def pause_task_for_approval(self, task_id, action_type, payload):
        task=next((x for x in self.tasks if x["id"]==task_id and x["tenant_id"]==self.principal.tenant_id),None)
        if not task or task.get("status")!="RUNNING": raise LookupError("Task not found or not RUNNING")
        task.update(status="AWAITING_APPROVAL",locked_by=None,lease_expires_at=None)
        return await self.create_approval(product_id=task.get("product_id"),employee_id=task["assignee_role_id"],
            action_type=action_type,payload=payload,expires_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=15))

    async def pause_run_for_approval(self, run_id, action_type, payload):
        run=await self.get_agent_run(run_id)
        if not run or run.get("execution_state") not in {"QUEUED","RUNNING"}: raise LookupError("Agent run not found or not resumable")
        run.update(status="awaiting_approval",execution_state="AWAITING_APPROVAL")
        return await self.create_approval(product_id=run.get("product_id"),employee_id=run["employee_id"],
            action_type=action_type,payload=payload,expires_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(minutes=15))

    async def consume_approval(self, approval_id, *, product_id, employee_id, action_type, payload, run_id):
        item=await self.validate_approval(approval_id,product_id=product_id,employee_id=employee_id,
            action_type=action_type,payload=payload)
        if item.get("consumed_at") is not None: raise PermissionError("Approval already consumed")
        item["consumed_at"]=datetime.now(timezone.utc); item["execution_run_id"]=run_id
        self._audit("approval.consumed","approval",approval_id,product_id,{"run_id":run_id})
        return item

    async def start_agent_run(self, run_id):
        run=await self.get_agent_run(run_id)
        if not run or run.get("execution_state") not in {"QUEUED","AWAITING_APPROVAL"}:
            raise PermissionError("Agent run is not in an executable state")
        run.update(execution_state="RUNNING",status="running")
        self._audit("agent_run.started","agent_run",run_id,run.get("product_id"))
        return run

    async def complete_agent_run(self, run_id, result):
        run=await self.get_agent_run(run_id)
        if not run or run.get("execution_state")!="RUNNING": raise PermissionError("Agent run is not running")
        run.update(execution_state="COMPLETED",status="completed",summary=result.get("summary",run.get("summary","")))
        self._audit("agent_run.completed","agent_run",run_id,run.get("product_id"))
        return run

    async def fail_agent_run(self, run_id, error):
        run=await self.get_agent_run(run_id)
        if not run or run.get("execution_state")!="RUNNING": raise PermissionError("Agent run is not running")
        run.update(execution_state="DEAD_LETTER",status="failed",summary=error[:4000])
        self._audit("agent_run.failed","agent_run",run_id,run.get("product_id"),{"error":error[:1000]})
        return run

    async def claim_adapter_idempotency(self, *, tenant_id, external_tenant_id, product_id, direction, idempotency_key, payload_hash, correlation_id):
        if tenant_id != self.principal.tenant_id: raise PermissionError("Adapter tenant mismatch")
        self._scope(product_id)
        item=next((x for x in self.adapter_idempotency if x["tenant_id"]==tenant_id and x["external_tenant_id"]==external_tenant_id and
          x["product_id"]==product_id and x["direction"]==direction and x["idempotency_key"]==idempotency_key),None)
        if item:
            if item["payload_hash"] != payload_hash: raise ValueError("Idempotency-Key reused with a different payload")
            self._audit("adapter.idempotency.replayed","adapter_idempotency",idempotency_key,product_id,
              {"direction":direction,"external_tenant_id":external_tenant_id,"correlation_id":correlation_id})
            return {"created":False,"response":item.get("response"),"status":item["status"]}
        self.adapter_idempotency.append({"tenant_id":tenant_id,"external_tenant_id":external_tenant_id,"product_id":product_id,"direction":direction,
          "idempotency_key":idempotency_key,"payload_hash":payload_hash,"status":"PROCESSING",
          "response":None,"correlation_id":correlation_id})
        self._audit("adapter.idempotency.claimed","adapter_idempotency",idempotency_key,product_id,
          {"direction":direction,"external_tenant_id":external_tenant_id,"correlation_id":correlation_id})
        return {"created":True,"response":None}

    async def complete_adapter_idempotency(self, *, tenant_id, external_tenant_id, product_id, direction, idempotency_key, response):
        if tenant_id != self.principal.tenant_id: raise PermissionError("Adapter tenant mismatch")
        self._scope(product_id)
        item=next((x for x in self.adapter_idempotency if x["tenant_id"]==tenant_id and x["external_tenant_id"]==external_tenant_id and
          x["product_id"]==product_id and x["direction"]==direction and x["idempotency_key"]==idempotency_key),None)
        if not item or item["status"]!="PROCESSING": raise PermissionError("Idempotency record unavailable or completed")
        item.update(status="COMPLETED",response=response)
        self._audit("adapter.idempotency.completed","adapter_idempotency",idempotency_key,product_id,
          {"direction":direction,"external_tenant_id":external_tenant_id})

    async def record_product_telemetry(self, *, tenant_id, external_tenant_id, product_id, event, correlation_id):
        if tenant_id != self.principal.tenant_id: raise PermissionError("Telemetry tenant mismatch")
        self._scope(product_id)
        if any(x["tenant_id"]==tenant_id and x["external_tenant_id"]==external_tenant_id and x["product_id"]==product_id and x["event_id"]==event["event_id"]
               for x in self.product_telemetry): return False
        self.product_telemetry.append({"tenant_id":tenant_id,"external_tenant_id":external_tenant_id,"product_id":product_id,**event,
                                       "correlation_id":correlation_id})
        self._audit("product.telemetry.received","product_telemetry",event["event_id"],product_id,
          {"event_type":event["event_type"],"recognised_revenue_inr":event["recognised_revenue_inr"],
           "correlation_id":correlation_id})
        return True

    async def record_model_usage(self, **usage):
        self._scope(usage.get("product_id"))
        self.model_usage.append({"id":str(uuid4()),"tenant_id":self.principal.tenant_id,**usage})
        self._audit("model.usage","model_usage",usage["run_id"],usage.get("product_id"),usage)

    async def commit_boundary(self):
        return None

    async def resume_agent_run(self, run_id, approval_id):
        run=await self.get_agent_run(run_id)
        approval=next((x for x in self.approvals if x["id"]==approval_id and x["tenant_id"]==self.principal.tenant_id),None)
        if run: self._scope(run.get("product_id"))
        if not run or run.get("execution_state")!="AWAITING_APPROVAL" or not approval or approval["status"]!="APPROVED" or approval["expires_at"]<=datetime.now(timezone.utc) or approval["product_id"]!=run.get("product_id") or approval["employee_id"]!=run["employee_id"] or approval.get("consumed_at"):
            raise PermissionError("Run cannot resume without matching live approval")
        run.update(status="planned",execution_state="QUEUED")
        self._audit("agent_run.resumed","agent_run",run_id,run.get("product_id"),{"approval_id":approval_id})
        return run

    async def upsert_prospect(self, *, product_id, dedupe_key, business_name, city, segment,
                             decision_maker_name=None, public_contact_channel=None, source_url,
                             evidence_urls, evidence_snippet="", confidence_score=0.5,
                             icp_fit_tier="UNASSESSED", is_synthetic=False, metadata=None):
        self._scope(product_id)
        if not 0 <= float(confidence_score) <= 1:
            raise ValueError("confidence_score must be between 0 and 1")
        rows=getattr(self,"prospects",None)
        if rows is None:
            self.prospects=[]; rows=self.prospects
        item=next((x for x in rows if x["tenant_id"]==self.principal.tenant_id
            and x["product_id"]==product_id and x["dedupe_key"]==dedupe_key),None)
        now=datetime.now(timezone.utc).isoformat()
        if item:
            item["evidence_urls"]=sorted(set(item.get("evidence_urls",[]))|set(evidence_urls))
            item["source_url"]=source_url
            if len(evidence_snippet)>len(item.get("evidence_snippet","")): item["evidence_snippet"]=evidence_snippet
            item["confidence_score"]=max(float(item["confidence_score"]),float(confidence_score))
            ranks={"LOW":1,"MEDIUM":2,"HIGH":3,"UNASSESSED":0}
            if ranks.get(icp_fit_tier,0)>ranks.get(item["icp_fit_tier"],0): item["icp_fit_tier"]=icp_fit_tier
            item["is_synthetic"]=bool(item["is_synthetic"] and is_synthetic)
            item["metadata"]={**item.get("metadata",{}),**(metadata or {})}
            item["last_evidenced_at"]=now; item["updated_at"]=now
        else:
            item={"id":str(uuid4()),"tenant_id":self.principal.tenant_id,"product_id":product_id,
                "dedupe_key":dedupe_key,"business_name":business_name,"city":city,"segment":segment,
                "decision_maker_name":decision_maker_name,"public_contact_channel":public_contact_channel,
                "source_url":source_url,"evidence_urls":sorted(set(evidence_urls)),
                "evidence_snippet":evidence_snippet,"discovered_at":now,"last_evidenced_at":now,
                "confidence_score":float(confidence_score),"icp_fit_tier":icp_fit_tier,
                "stage":"DISCOVERED","is_synthetic":bool(is_synthetic),"metadata":metadata or {},
                "created_at":now,"updated_at":now}
            rows.append(item)
        self._audit("prospect.upserted","prospect",item["id"],product_id,
            {"dedupe_key":dedupe_key,"evidence_count":len(item["evidence_urls"])})
        return item

    async def list_prospects(self, product_id=None, limit=200):
        self._scope(product_id)
        rows=getattr(self,"prospects",[])
        return [x for x in rows if x["tenant_id"]==self.principal.tenant_id
            and (x["product_id"]==product_id if product_id else
                 "*" in self.principal.permissions or x["product_id"] in self.principal.product_ids)][:limit]

    async def create_campaign(self, *, product_id, name, target_icp_segment, channel,
                              messaging_brief, ai_identity_disclosure, is_sandbox, created_by, metadata):
        self._scope(product_id)
        if not hasattr(self,"campaigns"): self.campaigns=[]
        if any(x["tenant_id"]==self.principal.tenant_id and x["product_id"]==product_id and x["name"]==name for x in self.campaigns):
            raise ValueError("Campaign name already exists in this tenant/product scope")
        now=datetime.now(timezone.utc).isoformat()
        item={"id":str(uuid4()),"tenant_id":self.principal.tenant_id,"product_id":product_id,
            "name":name,"target_icp_segment":target_icp_segment,"channel":channel,
            "messaging_brief":messaging_brief,"ai_identity_disclosure":ai_identity_disclosure,
            "status":"DRAFT","approval_id":None,"execution_run_id":None,"is_sandbox":is_sandbox,
            "metadata":metadata or {},"created_by":created_by,"created_at":now,"updated_at":now,
            "activated_at":None}
        self.campaigns.append(item); self._audit("campaign.created","campaign",item["id"],product_id); return item

    async def get_campaign(self, campaign_id):
        return next((x for x in getattr(self,"campaigns",[]) if x["id"]==str(campaign_id)
            and x["tenant_id"]==self.principal.tenant_id),None)

    async def list_campaigns(self, product_id=None, limit=200):
        self._scope(product_id)
        return [x for x in getattr(self,"campaigns",[]) if x["tenant_id"]==self.principal.tenant_id
            and (x["product_id"]==product_id if product_id else
                 "*" in self.principal.permissions or x["product_id"] in self.principal.product_ids)][:limit]

    async def request_campaign_approval(self, *, campaign_id, product_id, employee_id, messaging_brief):
        campaign=await self.get_campaign(campaign_id)
        if not campaign or campaign["product_id"]!=product_id: raise LookupError("Campaign not found")
        if campaign["status"] not in {"DRAFT","PAUSED"} or campaign["is_sandbox"]:
            raise PermissionError("Only a live draft campaign can request activation approval")
        approval=await self.create_approval(product_id=product_id,employee_id=employee_id,
            action_type="campaign.activate",payload={"campaign_id":str(campaign_id),"messaging_brief":messaging_brief},
            expires_at=datetime.now(timezone.utc)+__import__("datetime").timedelta(hours=24))
        campaign["status"]="AWAITING_APPROVAL"; campaign["updated_at"]=datetime.now(timezone.utc).isoformat()
        return approval

    async def transition_campaign(self, campaign_id, *, status, approval_id, execution_run_id):
        campaign=await self.get_campaign(campaign_id)
        if not campaign: raise LookupError("Campaign not found")
        approval=next((x for x in self.approvals if x["id"]==str(approval_id)
            and x["tenant_id"]==self.principal.tenant_id),None)
        if status=="APPROVED":
            if campaign["status"]!="AWAITING_APPROVAL" or not approval or approval["status"]!="APPROVED" \
                or approval.get("consumed_at") is None or approval.get("execution_run_id")!=str(execution_run_id) \
                or approval.get("action_type")!="campaign.activate" \
                or (approval.get("payload") or {}).get("campaign_id")!=str(campaign_id):
                raise PermissionError("Campaign requires a matching, consumed, single-use approval")
            campaign.update(status="APPROVED",approval_id=str(approval_id),execution_run_id=str(execution_run_id))
        elif status=="ACTIVE":
            if campaign["status"]!="APPROVED" or campaign["approval_id"]!=str(approval_id) \
                or campaign["execution_run_id"]!=str(execution_run_id):
                raise PermissionError("Campaign activation approval binding is invalid")
            campaign["status"]="ACTIVE"; campaign["activated_at"]=datetime.now(timezone.utc).isoformat()
        elif status=="PAUSED":
            campaign["status"]="PAUSED"
        else:
            raise ValueError("Unsupported campaign transition")
        campaign["updated_at"]=datetime.now(timezone.utc).isoformat()
        self._audit("campaign."+status.lower(),"campaign",str(campaign_id),campaign["product_id"],
            {"approval_id":str(approval_id),"execution_run_id":str(execution_run_id)})
        return campaign

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
