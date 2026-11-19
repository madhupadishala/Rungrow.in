"""Reporting repository extensions shared by durable and test repositories."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import text

class ReportingRepositoryMixin:
    async def list_product_telemetry(self):
        if hasattr(self, "session"):
            return await self._rows("""SELECT product_id,event_type,recognised_revenue_inr,
                occurred_at,source_ref,metadata,received_at FROM product_telemetry_events
                WHERE tenant_id=:tenant ORDER BY occurred_at""", {"tenant":self.principal.tenant_id})
        return [dict(x) for x in self.product_telemetry
                if x.get("tenant_id")==self.principal.tenant_id]

    async def list_investment_capital_events(self):
        if hasattr(self, "session"):
            return await self._rows("""SELECT id,event_type,investor_ref,amount_inr,is_received,
                approval_id,source_ref,metadata,occurred_at,recorded_at
                FROM investment_capital_events WHERE tenant_id=:tenant ORDER BY occurred_at""",
                {"tenant":self.principal.tenant_id})
        return [dict(x) for x in self.investment_capital_events
                if x.get("tenant_id")==self.principal.tenant_id]

    async def record_investment_capital_event(self, *, event_type, investor_ref, amount_inr,
            is_received, approval_id=None, source_ref=None, metadata=None, occurred_at=None):
        if event_type not in {"investor_lead","term_sheet_signed","commitment_approved","capital_received"}:
            raise ValueError("Unsupported investment event type")
        amount=float(amount_inr)
        if amount < 0: raise ValueError("Investment amount cannot be negative")
        if event_type=="capital_received" and (not is_received or amount<=0):
            raise ValueError("Capital received requires a positive amount and is_received=true")
        if event_type!="capital_received" and is_received:
            raise ValueError("Only capital_received events may be marked received")
        if event_type!="investor_lead" and not approval_id:
            raise ValueError("A verified approval_id is required for this investment event")
        when=occurred_at or datetime.now(timezone.utc)
        if hasattr(self,"session"):
            item=await self._one("""INSERT INTO investment_capital_events
              (tenant_id,event_type,investor_ref,amount_inr,is_received,approval_id,source_ref,
               metadata,occurred_at,created_by)
              VALUES (:tenant,:type,:investor,:amount,:received,:approval,:source,
               CAST(:metadata AS jsonb),:occurred,:actor) RETURNING *""",
              {"tenant":self.principal.tenant_id,"type":event_type,"investor":investor_ref,
               "amount":amount,"received":is_received,"approval":approval_id,"source":source_ref,
               "metadata":json.dumps(metadata or {}),"occurred":when,"actor":self.principal.user_id})
            await self.append_audit("investment.capital_event.recorded","investment_capital_event",
                str(item["id"]),None,{"event_type":event_type,"approval_id":approval_id})
            return item
        if event_type!="investor_lead":
            approval=next((a for a in self.approvals if a["id"]==approval_id
                and a["tenant_id"]==self.principal.tenant_id and a.get("status")=="APPROVED"
                and a.get("approver_id") and a.get("approver_id")!=a.get("requester_id")
                and a.get("expires_at")>datetime.now(timezone.utc)),None)
            if not approval: raise PermissionError("Investment event requires a verified, live approval")
        item={"id":str(uuid4()),"tenant_id":self.principal.tenant_id,"event_type":event_type,
          "investor_ref":investor_ref,"amount_inr":amount,"is_received":is_received,
          "approval_id":approval_id,"source_ref":source_ref,"metadata":metadata or {},
          "occurred_at":when,"recorded_at":datetime.now(timezone.utc),"created_by":self.principal.user_id}
        self.investment_capital_events.append(item)
        self._audit("investment.capital_event.recorded","investment_capital_event",item["id"],None,
          {"event_type":event_type,"approval_id":approval_id})
        return item

    async def save_ceo_briefing(self, *, snapshot, query_started_at, query_completed_at,
                                source_provenance, created_by):
        if hasattr(self,"session"):
            return await self._one("""INSERT INTO ceo_briefings
              (tenant_id,generated_at,query_started_at,query_completed_at,source_provenance,
               snapshot,created_by) VALUES (:tenant,:generated,:started,:completed,
               CAST(:sources AS jsonb),CAST(:snapshot AS jsonb),:actor) RETURNING id,generated_at""",
              {"tenant":self.principal.tenant_id,"generated":query_completed_at,
               "started":query_started_at,"completed":query_completed_at,
               "sources":json.dumps(source_provenance),"snapshot":json.dumps(snapshot,default=str),
               "actor":created_by})
        item={"id":str(uuid4()),"tenant_id":self.principal.tenant_id,
          "generated_at":query_completed_at,"query_started_at":query_started_at,
          "query_completed_at":query_completed_at,"source_provenance":list(source_provenance),
          "snapshot":snapshot,"created_by":created_by}
        self.ceo_briefings.append(item)
        return item

    async def latest_ceo_briefing(self):
        if hasattr(self,"session"):
            return await self._one("""SELECT id,generated_at,query_started_at,query_completed_at,
              source_provenance,snapshot FROM ceo_briefings WHERE tenant_id=:tenant
              ORDER BY generated_at DESC LIMIT 1""",{"tenant":self.principal.tenant_id})
        rows=[x for x in self.ceo_briefings if x["tenant_id"]==self.principal.tenant_id]
        return max(rows,key=lambda x:x["generated_at"]) if rows else None
