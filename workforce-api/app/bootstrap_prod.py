"""Idempotent RunGrow production tenant, adapter and provisional mission bootstrap."""
from __future__ import annotations
import asyncio
import json
import os
import re
from decimal import Decimal
from pathlib import Path
import asyncpg
from .migrate import asyncpg_dsn

TENANT_ID = "rungrow"
CANDIDATE_SHA_ENV = "RUNGROW_RELEASE_CANDIDATE_SHA"
DEFERRED_DUE_DATE = os.getenv("RELEASE_DEFERRED_REVIEW_DUE_DATE", "2027-02-28")
PORTFOLIO_PATH = Path(__file__).resolve().parents[2] / "data" / "workforce" / "product-portfolio.json"

REPOSITORIES = {
 "photo-studio-growth-os": ["madhupadishala/studio-growth-os", "madhupadishala/studio-x-photography"],
 "clinics-counter": ["madhupadishala/conversion-agent-platform"],
}
DEFERRED_RISK = {
 "photo-studio-billing": "No independent product repository and product-side v1 adapter has been verified. It remains outside the live cohort until backend, tenant isolation, HMAC, idempotency and telemetry acceptance are evidenced.",
 "entrepreneur-instagram-tools": "No independently deployable product repository or production backend has been verified. Live customer handoff, outreach and commercial claims remain disabled until product-specific integration is certified.",
 "pharma-software-utilities": "No independently deployable product repository or production backend has been verified. Regulated product workflows remain deferred until product-specific security, validation and integration evidence are accepted.",
 "artists-business-tools": "No independently deployable product repository or production backend has been verified. Live commercial operations remain deferred until its backend and adapter contract are certified.",
 "growtoys": "Growtoys remains DISCOVERY_ONLY. Discovery may continue, but live sales handoff, outreach and customer execution remain prohibited.",
}

def _portfolio():
    with PORTFOLIO_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    products = {item["product_id"]: item for item in data["products"]}
    if set(products) != set(REPOSITORIES) | set(DEFERRED_RISK):
        raise RuntimeError("Portfolio does not match the reviewed production bootstrap manifest")
    revenue_target = int(data["company_revenue_target"])
    capital_target = int(data["investment_capital_target"])
    if sum(int(item["annual_revenue_allocation"]) for item in products.values()) != revenue_target:
        raise RuntimeError("Revenue allocations do not sum to the company revenue target")
    return data, products, revenue_target, capital_target

async def bootstrap(database_url: str | None = None) -> dict:
    tenant_id = os.getenv("RUNGROW_INTERNAL_TENANT_ID", TENANT_ID).strip()
    candidate = os.getenv(CANDIDATE_SHA_ENV, "").strip().lower()
    environment = os.getenv("APP_ENV", "production").strip().lower()
    if not tenant_id:
        raise RuntimeError("RUNGROW_INTERNAL_TENANT_ID is required; the tenant slug is fixed as rungrow")
    if not re.fullmatch(r"[0-9a-f]{40,64}", candidate):
        raise RuntimeError(f"{CANDIDATE_SHA_ENV} must contain the immutable 40–64 character release commit SHA")
    if environment != "production":
        raise RuntimeError("Production bootstrap requires APP_ENV=production")
    dsn, ssl = asyncpg_dsn(database_url or os.getenv("DATABASE_URL", ""))
    _, products, revenue_target, capital_target = _portfolio()
    connection = await asyncpg.connect(dsn=dsn, ssl=ssl)
    try:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", 731904009)
            versions = await connection.fetch("SELECT version FROM schema_migrations")
            if "009" not in {str(row["version"]) for row in versions}:
                raise RuntimeError("Run python -m app.migrate first; migration 009 is not applied")
            await connection.execute("SELECT set_config('app.current_tenant_id',$1,true)", tenant_id)
            await connection.execute(
                """INSERT INTO wf_tenants(id,slug,name,status) VALUES($1,'rungrow','RunGrow','ACTIVE')
                   ON CONFLICT(id) DO UPDATE SET slug='rungrow',name='RunGrow',status='ACTIVE',updated_at=now()""",
                tenant_id,
            )
            registered = await connection.fetch(
                "SELECT product_id FROM wf_product_registry WHERE product_id=ANY($1::text[])",
                list(products),
            )
            missing = set(products) - {row["product_id"] for row in registered}
            if missing:
                raise RuntimeError("Product registry missing; run migrations first: " + ", ".join(sorted(missing)))

            for product_id, product in products.items():
                live = product_id in REPOSITORIES
                discovery = product_id == "growtoys"
                integration_status = "LIVE_BACKEND_WIRED" if live else ("DISCOVERY_ONLY" if discovery else "NO_REPO_YET")
                governance_mode = "LIVE" if live else ("DISCOVERY_ONLY" if discovery else "DEFERRED")
                repositories = REPOSITORIES.get(product_id, [])
                if live or discovery:
                    await connection.execute(
                        """INSERT INTO wf_product_bindings
                           (tenant_id,product_id,adapter_repository,adapter_branch,integration_status,
                            governance_mode,is_active,metadata)
                           VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
                           ON CONFLICT(tenant_id,product_id) DO UPDATE SET
                             adapter_repository=EXCLUDED.adapter_repository,
                             adapter_branch=EXCLUDED.adapter_branch,
                             integration_status=EXCLUDED.integration_status,
                             governance_mode=EXCLUDED.governance_mode,
                             is_active=EXCLUDED.is_active,metadata=EXCLUDED.metadata,updated_at=now()""",
                        tenant_id, product_id, repositories[0] if repositories else None,
                        "feat/rungrow-workforce-adapter-v1" if live else None,
                        integration_status, governance_mode, live,
                        json.dumps({"source_repositories": repositories, "adapter_version": "v1",
                                    "governance_mode": governance_mode,
                                    "live_handoff_allowed": live,
                                    "live_outreach_allowed": live,
                                    "http_block_status": 403 if discovery else None}),
                    )
                revenue_allocation = int(product["annual_revenue_allocation"])
                capital_allocation = (capital_target * revenue_allocation) // revenue_target
                await connection.execute(
                    """INSERT INTO wf_financial_baselines
                       (tenant_id,product_id,recognised_revenue_inr,raised_capital_inr,
                        revenue_target_allocation_inr,capital_target_allocation_inr,
                        revenue_target_total_inr,capital_target_total_inr,is_provisional,
                        allocation_type,baseline_note)
                       VALUES($1,$2,0,0,$3,$4,$5,$6,true,
                        'planning_allocation_not_forecast',$7)
                       ON CONFLICT(tenant_id,product_id) DO NOTHING""",
                    tenant_id, product_id, Decimal(revenue_allocation), Decimal(capital_allocation),
                    Decimal(revenue_target), Decimal(capital_target),
                    "Provisional planning allocation, not recognised revenue, capital received or a forecast.",
                )

                if product_id in DEFERRED_RISK:
                    rationale = DEFERRED_RISK[product_id]
                    evidence = {
                        "product_id": product_id,
                        "integration_status": integration_status,
                        "accepted_risk": rationale,
                        "risk_rationale": rationale,
                        "owner": product["owner_role_id"],
                        "due_date": DEFERRED_DUE_DATE,
                        "compensating_control": (
                            "Exclude this product from the live cohort and production execution. "
                            "Tenant isolation, HMAC, single-use human approval and fail-closed provider "
                            "controls remain mandatory. Growtoys remains DISCOVERY_ONLY."
                        ),
                        "cohort": "portfolio",
                    }
                    await connection.execute(
                        """INSERT INTO release_gate_signoffs
                           (tenant_id,release_layer,gate_name,status,evidence,verifier_type,verifier_id,
                            candidate_sha,environment,cohort_id,scope_key)
                           VALUES($1,'RELEASE_2_PRODUCTS','BUSINESS','DEFERRED_WITH_ACCEPTANCE',
                            $2::jsonb,'AUTOMATED','bootstrap_prod',$3,'production','portfolio',$4)
                           ON CONFLICT DO NOTHING""",
                        tenant_id, json.dumps(evidence), candidate, product_id,
                    )

            revenue_sum = await connection.fetchval(
                "SELECT sum(revenue_target_allocation_inr) FROM wf_financial_baselines WHERE tenant_id=$1",
                tenant_id,
            )
            capital_sum = await connection.fetchval(
                "SELECT sum(capital_target_allocation_inr) FROM wf_financial_baselines WHERE tenant_id=$1",
                tenant_id,
            )
            if Decimal(revenue_sum) != Decimal(revenue_target) or Decimal(capital_sum) != Decimal(capital_target):
                raise RuntimeError("Product financial allocations do not reconcile to both company targets")
            counts = await connection.fetchrow(
                """SELECT
                   (SELECT count(*) FROM wf_product_bindings WHERE tenant_id=$1) AS bindings,
                   (SELECT count(*) FROM wf_financial_baselines WHERE tenant_id=$1) AS baselines,
                   (SELECT count(*) FROM release_gate_signoffs WHERE tenant_id=$1 AND cohort_id='portfolio'
                    AND status='DEFERRED_WITH_ACCEPTANCE') AS deferred""",
                tenant_id,
            )
            return {
                "tenant_id": tenant_id, "tenant_slug": "rungrow", "name": "RunGrow",
                "live_products": sorted(REPOSITORIES), "deferred_products": sorted(DEFERRED_RISK),
                "discovery_only": ["growtoys"], "product_bindings": counts["bindings"],
                "financial_baselines": counts["baselines"], "product_allocations": counts["baselines"],
                "deferred_signoffs": counts["deferred"], "is_provisional": True,
                "recognised_revenue_inr": 0, "raised_capital_inr": 0,
                "revenue_target_inr": revenue_target, "capital_target_inr": capital_target,
                "idempotent": True,
            }
    finally:
        await connection.close()

def main() -> None:
    print(json.dumps(asyncio.run(bootstrap()), indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
