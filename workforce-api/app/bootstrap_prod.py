"""Idempotently provision RunGrow's production tenant, adapter cohort and mission baseline."""
from __future__ import annotations
import asyncio
import json
import os
import re
from pathlib import Path
import asyncpg
from .migrate import asyncpg_dsn

ROOT=Path(__file__).resolve().parents[2]
PORTFOLIO_PATH=ROOT/"data"/"workforce"/"product-portfolio.json"
ACTIVE_BINDINGS={
 "photo-studio-growth-os":{"status":"LIVE_BACKEND_WIRED","repositories":["madhupadishala/studio-growth-os","madhupadishala/studio-x-photography"],"metadata":{"cohort_id":"flagship-production","adapter_version":"v1"}},
 "clinics-counter":{"status":"LIVE_BACKEND_WIRED","repositories":["madhupadishala/conversion-agent-platform"],"metadata":{"cohort_id":"flagship-production","adapter_version":"v1"}},
 "growtoys":{"status":"DISCOVERY_ONLY","repositories":[],"metadata":{"cohort_id":"portfolio-expansion","live_handoff_allowed":False,"live_outreach_allowed":False,"http_block_status":403}},
}
DEFERRED_PRODUCTS={
 "photo-studio-billing":"No independent product repository or product-side v1 adapter has been verified. Production writes, handoff and commercial claims remain disabled until the backend is independently deployed and its v1 contract is certified.",
 "entrepreneur-instagram-tools":"No independently deployable product repository or production backend has been verified. Live customer handoff and outreach remain disabled until product-specific integration and acceptance are complete.",
 "pharma-software-utilities":"No independently deployable product repository or production backend has been verified. Product-specific security, validation and integration evidence must be accepted before production activation.",
 "artists-business-tools":"No independently deployable product repository or production backend has been verified. Live commercial operations remain deferred until its backend and adapter contract are certified.",
}
DEFERRED_DUE_DATE=os.getenv("RELEASE_DEFERRED_REVIEW_DUE_DATE","2027-02-28")

def _portfolio():
 with PORTFOLIO_PATH.open("r",encoding="utf-8") as f: data=json.load(f)
 products={p["product_id"]:p for p in data["products"]}
 expected=set(ACTIVE_BINDINGS)|set(DEFERRED_PRODUCTS)|{"growtoys"}
 if set(products)!=expected: raise RuntimeError("Portfolio product set differs from approved production bootstrap manifest")
 if sum(int(p["annual_revenue_allocation"]) for p in products.values())!=int(data["company_revenue_target"]):
  raise RuntimeError("Product revenue allocations do not sum to the company target")
 return data,products

async def bootstrap(database_url: str|None=None):
 tenant_id=os.getenv("RUNGROW_INTERNAL_TENANT_ID","").strip()
 candidate=os.getenv("RUNGROW_RELEASE_CANDIDATE_SHA","").strip().lower()
 environment=os.getenv("APP_ENV","production").strip().lower()
 if not tenant_id: raise RuntimeError("RUNGROW_INTERNAL_TENANT_ID is required; refusing to invent a tenant binding")
 if not re.fullmatch(r"[0-9a-f]{40,64}",candidate):
  raise RuntimeError("RUNGROW_RELEASE_CANDIDATE_SHA must be the immutable 40–64 character release commit SHA")
 if environment!="production": raise RuntimeError("Production bootstrap requires APP_ENV=production")
 dsn,ssl=asyncpg_dsn(database_url or os.getenv("DATABASE_URL",""))
 portfolio,products=_portfolio()
 revenue_target=int(portfolio["company_revenue_target"])
 capital_target=int(portfolio["investment_capital_target"])
 conn=await asyncpg.connect(dsn=dsn,ssl=ssl)
 try:
  async with conn.transaction():
   await conn.execute("SELECT pg_advisory_xact_lock($1)",731904009)
   await conn.execute("""INSERT INTO wf_tenants(tenant_id,slug,name,status)
    VALUES($1,'rungrow','RunGrow','ACTIVE')
    ON CONFLICT(tenant_id) DO UPDATE SET slug='rungrow',name='RunGrow',status='ACTIVE',updated_at=now()""",tenant_id)
   await conn.execute("SELECT set_config('app.current_tenant_id',$1,true)",tenant_id)
   registered=await conn.fetch("SELECT product_id FROM wf_product_registry WHERE product_id=ANY($1::text[])",list(products))
   missing=set(products)-{row["product_id"] for row in registered}
   if missing: raise RuntimeError("Run python -m app.migrate first; product registry missing: "+", ".join(sorted(missing)))
   for pid,binding in ACTIVE_BINDINGS.items():
    await conn.execute("""INSERT INTO wf_product_adapter_bindings
     (tenant_id,product_id,adapter_version,integration_status,source_repositories,metadata)
     VALUES($1,$2,'v1',$3,$4::jsonb,$5::jsonb)
     ON CONFLICT(tenant_id,product_id) DO UPDATE SET adapter_version=EXCLUDED.adapter_version,
     integration_status=EXCLUDED.integration_status,source_repositories=EXCLUDED.source_repositories,
     metadata=EXCLUDED.metadata,updated_at=now()""",
     tenant_id,pid,binding["status"],json.dumps(binding["repositories"]),json.dumps(binding["metadata"]))
   await conn.execute("""INSERT INTO wf_mission_baselines
    (tenant_id,currency,revenue_target_inr,capital_target_inr,recognised_revenue_inr,raised_capital_inr,
     is_provisional,allocation_type,metadata)
    VALUES($1,'INR',$2,$3,0,0,true,'planning_allocation_not_forecast',$4::jsonb)
    ON CONFLICT(tenant_id) DO UPDATE SET currency='INR',revenue_target_inr=EXCLUDED.revenue_target_inr,
    capital_target_inr=EXCLUDED.capital_target_inr,is_provisional=true,
    allocation_type=EXCLUDED.allocation_type,metadata=EXCLUDED.metadata,updated_at=now()""",
    tenant_id,revenue_target,capital_target,json.dumps({"source":"product-portfolio.json",
    "provisional":True,"recognised_revenue_inr":0,"raised_capital_inr":0,
    "target_type":"management_planning_allocation_not_forecast"}))
   for pid,product in products.items():
    rev=int(product["annual_revenue_allocation"])
    cap=(capital_target*rev)//revenue_target
    await conn.execute("""INSERT INTO wf_mission_product_allocations
     (tenant_id,product_id,revenue_target_inr,capital_target_inr,is_provisional,allocation_type)
     VALUES($1,$2,$3,$4,true,'planning_allocation_not_forecast')
     ON CONFLICT(tenant_id,product_id) DO UPDATE SET revenue_target_inr=EXCLUDED.revenue_target_inr,
     capital_target_inr=EXCLUDED.capital_target_inr,is_provisional=true,
     allocation_type=EXCLUDED.allocation_type,updated_at=now()""",tenant_id,pid,rev,cap)
   allocated=await conn.fetchval("SELECT sum(capital_target_inr) FROM wf_mission_product_allocations WHERE tenant_id=$1",tenant_id)
   if allocated!=capital_target:
    await conn.execute("""UPDATE wf_mission_product_allocations SET capital_target_inr=capital_target_inr+$3,
     updated_at=now() WHERE tenant_id=$1 AND product_id='growtoys'""",tenant_id,"growtoys",capital_target-allocated)
   for pid,risk in DEFERRED_PRODUCTS.items():
    product=products[pid]
    evidence={"cohort_id":"portfolio-expansion","product_id":pid,"repository_status":"NO_REPO_YET",
     "accepted_risk":risk,"owner":product["owner_role_id"],"due_date":DEFERRED_DUE_DATE,
     "compensating_control":"Exclude from live cohort; disable product-side writes, customer handoff, outreach and revenue claims until an independently deployed adapter passes PostgreSQL contract CI and receives product-owner acceptance.",
     "risk_category":"PRODUCT_BACKEND_NOT_INTEGRATED"}
    await _record_deferral(conn,tenant_id,candidate,pid,evidence)
   product=products["growtoys"]
   evidence={"cohort_id":"portfolio-expansion","product_id":"growtoys","governance_mode":"DISCOVERY_ONLY",
    "accepted_risk":"Growtoys remains discovery-only and is not approved for production sales handoff or outreach.",
    "owner":product["owner_role_id"],"due_date":DEFERRED_DUE_DATE,
    "compensating_control":"Enforce DISCOVERY_ONLY in the adapter registry and HTTP 403 for live handoff/outreach; prohibit outbound communication and recognised revenue.",
    "risk_category":"DISCOVERY_ONLY_NO_LIVE_SALES","live_handoff_allowed":False,"live_outreach_allowed":False}
   await _record_deferral(conn,tenant_id,candidate,"growtoys",evidence)
  counts=await conn.fetchrow("""SELECT
   (SELECT count(*) FROM wf_product_adapter_bindings WHERE tenant_id=$1) bindings,
   (SELECT count(*) FROM wf_mission_product_allocations WHERE tenant_id=$1) allocations,
   (SELECT count(*) FROM release_gate_signoffs WHERE tenant_id=$1 AND status='DEFERRED_WITH_ACCEPTANCE'
    AND cohort_id='portfolio-expansion') deferred_signoffs""",tenant_id)
  return {"tenant_id":tenant_id,"tenant_slug":"rungrow","name":"RunGrow",
   "live_products":["photo-studio-growth-os","clinics-counter"],"discovery_only":["growtoys"],
   "provisional":True,"recognised_revenue_inr":0,"raised_capital_inr":0,
   "revenue_target_inr":revenue_target,"capital_target_inr":capital_target,
   "product_bindings":counts["bindings"],"product_allocations":counts["allocations"],
   "deferred_signoffs":counts["deferred_signoffs"],"idempotent":True}
 finally: await conn.close()

async def _record_deferral(conn,tenant_id,candidate,product_id,evidence):
 await conn.execute("""INSERT INTO release_gate_signoffs
  (tenant_id,release_layer,gate_name,status,evidence,verifier_type,verifier_id,candidate_sha,environment,cohort_id,scope_key)
  VALUES($1,'RELEASE_2_PRODUCTS','INTEGRATION','DEFERRED_WITH_ACCEPTANCE',$2::jsonb,
   'AUTOMATED','bootstrap_prod',$3,'production','portfolio-expansion',$4)
  ON CONFLICT(tenant_id,release_layer,gate_name,cohort_id,scope_key,candidate_sha) DO NOTHING""",
  tenant_id,json.dumps(evidence),candidate,product_id)

def main():
 print(json.dumps(asyncio.run(bootstrap()),indent=2,sort_keys=True))

if __name__=="__main__": main()
