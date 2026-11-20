"""Idempotent production seed for RunGrow primary tenant and go-live cohort.
Run after python -m app.migrate; safe to execute repeatedly.
"""
from __future__ import annotations
import asyncio, json, os
from decimal import Decimal
import asyncpg
from .migrate import asyncpg_dsn

TENANT_ID = "rungrow"
CANDIDATE_SHA = os.getenv("RUN_GROW_RELEASE_CANDIDATE_SHA", "0000000000000000000000000000000000000000")
ENVIRONMENT = os.getenv("APP_ENV", "production")
PRODUCTS = (
 {"product_id":"photo-studio-growth-os","display_name":"Photo Studio Growth OS / Growth Pro","revenue":12500000,"capital":12500000,"integration_status":"LIVE_BACKEND_WIRED","governance_mode":"LIVE","is_active":True,"adapter_repository":"madhupadishala/studio-growth-os","adapter_branch":"feat/rungrow-workforce-adapter-v1","source_repositories":["madhupadishala/studio-growth-os","madhupadishala/studio-x-photography"]},
 {"product_id":"photo-studio-billing","display_name":"Photo Studio Billing Software","revenue":8500000,"capital":8500000,"integration_status":"NO_REPO_YET","governance_mode":"DEFERRED","is_active":False,"owner":"pgm-photo-studio-billing"},
 {"product_id":"clinics-counter","display_name":"Clinics / Counter Software","revenue":9000000,"capital":9000000,"integration_status":"LIVE_BACKEND_WIRED","governance_mode":"LIVE","is_active":True,"adapter_repository":"madhupadishala/conversion-agent-platform","adapter_branch":"feat/rungrow-workforce-adapter-v1","source_repositories":["madhupadishala/conversion-agent-platform"]},
 {"product_id":"entrepreneur-instagram-tools","display_name":"Entrepreneur / Instagram Business Tools","revenue":6500000,"capital":6500000,"integration_status":"NO_REPO_YET","governance_mode":"DEFERRED","is_active":False,"owner":"pgm-entrepreneur-tools"},
 {"product_id":"pharma-software-utilities","display_name":"Pharma Software & Utilities","revenue":6000000,"capital":6000000,"integration_status":"NO_REPO_YET","governance_mode":"DEFERRED","is_active":False,"owner":"pgm-pharma"},
 {"product_id":"artists-business-tools","display_name":"Artists Business Tools","revenue":3500000,"capital":3500000,"integration_status":"NO_REPO_YET","governance_mode":"DEFERRED","is_active":False,"owner":"pgm-artists-tools"},
 {"product_id":"growtoys","display_name":"Growtoys","revenue":4000000,"capital":4000000,"integration_status":"DISCOVERY_ONLY","governance_mode":"DISCOVERY_ONLY","is_active":False,"owner":"pgm-growtoys"},
)
DEFERRED_RATIONALES = {
 "photo-studio-billing":"No independent product repository and product-side v1 adapter has been verified. Excluded from flagship production until its backend contract, tenant isolation, idempotency and telemetry CI are accepted.",
 "entrepreneur-instagram-tools":"No independently deployable product repository or production backend has been verified. No live customer handoff or commercial claim is authorised until its product adapter is certified.",
 "pharma-software-utilities":"No independently deployable product repository or production backend has been verified. Regulated workflows remain outside this production cohort until product-specific controls and evidence are accepted.",
 "artists-business-tools":"No independently deployable product repository or production backend has been verified. Live commercial operations remain deferred until backend and adapter contract are certified.",
 "growtoys":"Growtoys remains DISCOVERY_ONLY. Discovery may continue, but live sales handoff, outreach and customer execution remain prohibited.",
}

async def bootstrap(database_url: str | None = None) -> dict:
    dsn, ssl = asyncpg_dsn(database_url or os.getenv("DATABASE_URL", ""))
    conn = await asyncpg.connect(dsn=dsn, ssl=ssl)
    try:
        async with conn.transaction():
            versions = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
            if "009" not in {str(row["version"]) for row in versions}:
                raise RuntimeError("Run python -m app.migrate first; migration 009 is not applied")
            await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", TENANT_ID)
            await conn.execute("""INSERT INTO wf_tenants(id,slug,name,status) VALUES($1,'rungrow','RunGrow','ACTIVE')
                ON CONFLICT(id) DO UPDATE SET slug=EXCLUDED.slug,name=EXCLUDED.name,status='ACTIVE',updated_at=now()""", TENANT_ID)
            for p in PRODUCTS:
                await conn.execute("""INSERT INTO wf_product_registry(product_id,display_name,is_provisional,baseline_status)
                    VALUES($1,$2,true,'unreconciled') ON CONFLICT(product_id) DO NOTHING""", p["product_id"], p["display_name"])
                metadata = json.dumps({"source_repositories":p.get("source_repositories",[]),"bootstrap_version":1})
                await conn.execute("""INSERT INTO wf_product_bindings
                    (tenant_id,product_id,adapter_repository,adapter_branch,integration_status,governance_mode,is_active,metadata)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
                    ON CONFLICT(tenant_id,product_id) DO UPDATE SET
                    adapter_repository=EXCLUDED.adapter_repository,adapter_branch=EXCLUDED.adapter_branch,
                    integration_status=EXCLUDED.integration_status,governance_mode=EXCLUDED.governance_mode,
                    is_active=EXCLUDED.is_active,metadata=EXCLUDED.metadata,updated_at=now()""",
                    TENANT_ID,p["product_id"],p.get("adapter_repository"),p.get("adapter_branch"),
                    p["integration_status"],p["governance_mode"],p["is_active"],metadata)
                await conn.execute("""INSERT INTO wf_financial_baselines
                    (tenant_id,product_id,recognised_revenue_inr,raised_capital_inr,
                     revenue_target_allocation_inr,capital_target_allocation_inr,
                     revenue_target_total_inr,capital_target_total_inr,is_provisional,allocation_type,baseline_note)
                    VALUES($1,$2,0,0,$3,$4,50000000,50000000,true,
                     'planning_allocation_not_forecast',$5)
                    ON CONFLICT(tenant_id,product_id) DO NOTHING""",
                    TENANT_ID,p["product_id"],Decimal(p["revenue"]),Decimal(p["capital"]),
                    "Provisional management planning allocation only; not recognised revenue, capital received, forecast, or customer-validated evidence.")
                if p["integration_status"] in {"NO_REPO_YET","DISCOVERY_ONLY"}:
                    rationale = DEFERRED_RATIONALES[p["product_id"]]
                    evidence = json.dumps({
                        "product_id":p["product_id"],"integration_status":p["integration_status"],
                        "accepted_risk":rationale,"risk_rationale":rationale,
                        "owner":p.get("owner","pgm-growtoys"),
                        "due_date":os.getenv("RELEASE_DEFERRED_REVIEW_DUE_DATE","2027-01-31"),
                        "compensating_control":"Exclude from live cohort and production execution. Tenant isolation, HMAC, approval boundaries and fail-closed provider controls remain mandatory. Growtoys live handoff/outreach remains blocked.",
                        "scope":"Product-specific deferral only; no security or reliability gate is deferred.",
                        "cohort":"portfolio"})
                    await conn.execute("""INSERT INTO release_gate_signoffs
                        (tenant_id,release_layer,gate_name,status,evidence,verifier_type,verifier_id,
                         candidate_sha,environment,cohort_id,scope_key)
                        VALUES($1,'RELEASE_2_PRODUCTS','BUSINESS','DEFERRED_WITH_ACCEPTANCE',
                         $2::jsonb,'HUMAN','founder-approved-scope',$3,$4,'portfolio',$5)
                        ON CONFLICT(tenant_id,release_layer,gate_name,cohort_id,scope_key,candidate_sha) DO NOTHING""",
                        TENANT_ID,evidence,CANDIDATE_SHA,ENVIRONMENT,p["product_id"])
        return {"tenant_id":TENANT_ID,"tenant_slug":"rungrow","products_registered":len(PRODUCTS),
            "live_products":["photo-studio-growth-os","clinics-counter"],
            "deferred_products":[p["product_id"] for p in PRODUCTS if p["integration_status"] in {"NO_REPO_YET","DISCOVERY_ONLY"}],
            "financial_baselines":len(PRODUCTS),"recognised_revenue_inr":0,"raised_capital_inr":0,
            "revenue_target_inr":50000000,"capital_target_inr":50000000,"idempotent":True}
    finally:
        await conn.close()

def main() -> None:
    print(json.dumps(asyncio.run(bootstrap()),indent=2,sort_keys=True))

if __name__ == "__main__":
    main()
