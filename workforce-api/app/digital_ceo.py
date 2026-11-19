"""Deterministic, repository-backed Digital CEO reporting.

No synthetic metrics are introduced here. Missing evidence is represented as
None/zero with provenance flags; target allocations are explicitly provisional.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

REVENUE_TARGET_INR = 50_000_000
CAPITAL_TARGET_INR = 50_000_000
PRODUCT_ALLOCATIONS = {
 "photo-studio-growth-os": 12_500_000,
 "photo-studio-billing": 8_500_000,
 "clinics-counter": 9_000_000,
 "entrepreneur-instagram-tools": 6_500_000,
 "pharma-software-utilities": 6_000_000,
 "artists-business-tools": 3_500_000,
 "growtoys": 4_000_000,
}
PRODUCT_LABELS = {
 "photo-studio-growth-os":"Photo Studio Growth OS",
 "photo-studio-billing":"Photo Studio Billing",
 "clinics-counter":"Clinics / Counter",
 "entrepreneur-instagram-tools":"Entrepreneur / Instagram Tools",
 "pharma-software-utilities":"Pharma Software & Utilities",
 "artists-business-tools":"Artists' Business Tools",
 "growtoys":"Growtoys",
}

def _dt(value):
    if isinstance(value, datetime): return value
    if isinstance(value,str):
        try: return datetime.fromisoformat(value.replace("Z","+00:00"))
        except ValueError: return None
    return None

def _iso(value):
    dt=_dt(value)
    return dt.astimezone(timezone.utc).isoformat() if dt else None

class DigitalCEOService:
    def __init__(self, repository, registry, products):
        self.repo,self.registry,self.products=repository,registry,products

    async def _rows(self, name, *args):
        method=getattr(self.repo,name,None)
        if not method: return []
        result=await method(*args)
        return result or []

    async def briefing(self, generated_by="system"):
        started=datetime.now(timezone.utc)
        tasks=await self._rows("list_tasks")
        approvals=await self._rows("list_approvals")
        telemetry=await self._rows("list_product_telemetry")
        capital=await self._rows("list_investment_capital_events")
        now=datetime.now(timezone.utc)
        task_rows=[]
        for product_id in PRODUCT_ALLOCATIONS:
            scoped=[t for t in tasks if t.get("product_id")==product_id]
            task_rows.append({"product_id":product_id,"product":PRODUCT_LABELS[product_id],
              "completed":sum(str(t.get("status","")).upper() in {"COMPLETED","DONE"} for t in scoped),
              "active":sum(str(t.get("status","")).upper() in {"QUEUED","RUNNING","RETRY_SCHEDULED","AWAITING_APPROVAL"} for t in scoped),
              "dead_lettered":sum(str(t.get("status","")).upper() in {"DEAD_LETTER","FAILED"} for t in scoped)})
        corp=[t for t in tasks if not t.get("product_id")]
        task_rows.append({"product_id":None,"product":"Shared corporate departments",
          "completed":sum(str(t.get("status","")).upper() in {"COMPLETED","DONE"} for t in corp),
          "active":sum(str(t.get("status","")).upper() in {"QUEUED","RUNNING","RETRY_SCHEDULED","AWAITING_APPROVAL"} for t in corp),
          "dead_lettered":sum(str(t.get("status","")).upper() in {"DEAD_LETTER","FAILED"} for t in corp)})
        by_product={p:Decimal("0") for p in PRODUCT_ALLOCATIONS}
        for event in telemetry:
            if event.get("event_type")=="invoice_recognised":
                pid=event.get("product_id")
                if pid in by_product: by_product[pid]+=Decimal(str(event.get("recognised_revenue_inr") or 0))
        revenue=sum(by_product.values(),Decimal("0"))
        revenue_rows=[{"product_id":pid,"product":PRODUCT_LABELS[pid],
          "actual_recognised_inr":float(by_product[pid]),"target_inr":PRODUCT_ALLOCATIONS[pid],
          "allocation_label":"PROVISIONAL ALLOCATION","is_provisional":True,
          "data_source":"product_telemetry_events:event_type=invoice_recognised",
          "is_synthetic":False} for pid in PRODUCT_ALLOCATIONS]
        received=sum((Decimal(str(e.get("amount_inr") or 0)) for e in capital
          if e.get("event_type")=="capital_received" and e.get("is_received") is True),Decimal("0"))
        committed=sum((Decimal(str(e.get("amount_inr") or 0)) for e in capital
          if e.get("event_type") in {"term_sheet_signed","commitment_approved"}),Decimal("0"))
        pending=[]
        for a in approvals:
            if str(a.get("status","")).upper()!="PENDING": continue
            expiry=_dt(a.get("expires_at"))
            pending.append({"approval_id":str(a.get("id")),"product_id":a.get("product_id"),
             "employee_id":a.get("employee_id"),"action_type":a.get("action_type"),
             "created_at":_iso(a.get("created_at")),"expires_at":_iso(expiry),
             "age_seconds":max(0,int((now-(_dt(a.get("created_at")) or now)).total_seconds())),
             "seconds_until_expiry":max(0,int((expiry-now).total_seconds())) if expiry else None})
        pending.sort(key=lambda x:x["expires_at"] or "9999")
        risks=[]
        for row in task_rows:
            if row["dead_lettered"]: risks.append({"severity":"high","type":"dead_letter","scope":row["product"],"count":row["dead_lettered"]})
            if row["active"]>=10: risks.append({"severity":"medium","type":"queue_bottleneck","scope":row["product"],"count":row["active"]})
        actions=[]
        if pending: actions.append({"priority":"P1","action":"Review pending human approvals before expiry","count":len(pending)})
        if any(r["type"]=="dead_letter" for r in risks): actions.append({"priority":"P1","action":"Triage dead-lettered work and repair root causes"})
        if not actions: actions.append({"priority":"P2","action":"Review product revenue evidence and next conversion bottleneck"})
        completed_at=datetime.now(timezone.utc)
        snapshot={"generated_at":completed_at.isoformat(),"query_started_at":started.isoformat(),
          "query_completed_at":completed_at.isoformat(),"tasks":task_rows,
          "operating_revenue":{"actual_inr":float(revenue),"target_inr":REVENUE_TARGET_INR,
            "remaining_inr":max(0,REVENUE_TARGET_INR-float(revenue)),"data_source":"product_telemetry_events",
            "event_filter":"invoice_recognised","is_synthetic":False,"as_of":completed_at.isoformat(),
            "by_product":revenue_rows},
          "investment_capital":{"received_inr":float(received),"committed_pipeline_inr":float(committed),
            "target_inr":CAPITAL_TARGET_INR,"remaining_received_inr":max(0,CAPITAL_TARGET_INR-float(received)),
            "data_source":"investment_capital_events","is_synthetic":False,"as_of":completed_at.isoformat()},
          "pending_approvals":pending,"risks":risks,"recommended_actions":actions,
          "source_provenance":["wf_tasks","wf_approvals","product_telemetry_events","investment_capital_events"]}
        await self.repo.save_ceo_briefing(snapshot=snapshot,query_started_at=started,
          query_completed_at=completed_at,source_provenance=snapshot["source_provenance"],created_by=generated_by)
        return snapshot
