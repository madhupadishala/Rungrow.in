"""Cohort-aware, evidence-driven seven-gate release evaluator.

Product deferrals are scoped to the portfolio-expansion cohort and never satisfy
shared security, approval, reliability, or external-gateway controls.
"""
from __future__ import annotations

import os
from datetime import date
from .config import settings

LAYERS=("RELEASE_1_WORKFORCE","RELEASE_2_PRODUCTS","RELEASE_3_COMMERCIAL")
GATES=(
    ("ARCHITECTURE_RELEASE_INTEGRITY","ENGINEERING"),
    ("SECURITY_TENANT_ISOLATION","SECURITY"),
    ("BACKEND_INTEGRATION_READINESS","INTEGRATION"),
    ("AI_WORKFORCE_ACCEPTANCE","AI_GOVERNANCE"),
    ("RELIABILITY_DISASTER_RECOVERY","RELIABILITY"),
    ("PRODUCT_COMMERCIAL_READINESS","BUSINESS"),
    ("FINAL_RELEASE_AUTHORIZATION","OPERATIONS"),
)

FLAGSHIP_COHORT="flagship-production"
PORTFOLIO_COHORT="portfolio"
EXPANSION_COHORT="portfolio-expansion"
COHORT_PRODUCTS={
    FLAGSHIP_COHORT: ("photo-studio-growth-os","clinics-counter"),
    PORTFOLIO_COHORT: (
        "photo-studio-growth-os","photo-studio-billing","clinics-counter",
        "entrepreneur-instagram-tools","pharma-software-utilities",
        "artists-business-tools","growtoys",
    ),
}
DEFERABLE_PRODUCTS={
    "photo-studio-billing","entrepreneur-instagram-tools",
    "pharma-software-utilities","artists-business-tools","growtoys",
}
DEFERABLE_GATES={"INTEGRATION","BUSINESS"}
ACCEPTANCE_FIELDS=("accepted_risk","owner","due_date","compensating_control")


def _has_evidence(evidence, key):
    value=(evidence or {}).get(key)
    return bool(value) and (not isinstance(value,dict) or all(bool(v) for v in value.values()))


def _valid_deferral(item):
    evidence=item.get("evidence") or {}
    if item.get("status")!="DEFERRED_WITH_ACCEPTANCE":
        return False
    if item.get("release_layer")!="RELEASE_2_PRODUCTS" or item.get("gate_name") not in DEFERABLE_GATES:
        return False
    product_id=evidence.get("product_id")
    if product_id not in DEFERABLE_PRODUCTS:
        return False
    if evidence.get("cohort_id")!=EXPANSION_COHORT:
        return False
    if not all(isinstance(evidence.get(key),str) and evidence[key].strip() for key in ACCEPTANCE_FIELDS):
        return False
    try:
        date.fromisoformat(evidence["due_date"])
    except (TypeError,ValueError):
        return False
    if product_id=="growtoys" and evidence.get("governance_mode")!="DISCOVERY_ONLY":
        return False
    if product_id!="growtoys" and evidence.get("repository_status")!="NO_REPO_YET":
        return False
    return True


def _hard_security_missing():
    missing=[]
    if not (os.getenv("JWT_JWKS_URL") or settings.jwt_jwks_url
            or os.getenv("JWT_PUBLIC_KEY") or settings.jwt_public_key):
        missing.append("Production OIDC JWKS URL or asymmetric JWT public key")
    if len(os.getenv("STUDIO_GROWTH_OS_SERVICE_SECRET","").encode())<32:
        missing.append("32-byte minimum product service HMAC secret")
    if len(os.getenv("RUNGROW_ADAPTER_WEBHOOK_SECRET","").encode())<32:
        missing.append("32-byte minimum outbound webhook HMAC secret")
    return missing


class ReleaseReadinessService:
    def __init__(self,repository,*,adapter_registry=None,now=None):
        self.repository=repository
        self.adapter_registry=adapter_registry
        self.now=now

    async def evaluate(self,cohort_id: str=PORTFOLIO_COHORT):
        if cohort_id not in COHORT_PRODUCTS:
            raise ValueError("Unknown release cohort")
        signoffs=await self.repository.list_release_gate_signoffs()
        heartbeats=await self.repository.list_worker_heartbeats(stale_after_seconds=90)
        migration_state=await self.repository.verify_migration_chain()
        adapters=self.adapter_registry.readiness() if self.adapter_registry else []
        registered=set(self.adapter_registry.product_ids) if self.adapter_registry else set()
        product_ids=list(COHORT_PRODUCTS[cohort_id])
        valid_deferrals={}
        for item in signoffs:
            if _valid_deferral(item):
                product_id=item["evidence"]["product_id"]
                current=valid_deferrals.get(product_id)
                if current is None or item.get("occurred_at","")>current.get("occurred_at",""):
                    valid_deferrals[product_id]=item

        # Only cohort/global gate signoffs can satisfy gate-level controls.
        # Product-specific deferrals are evaluated independently below.
        cohort_signoffs=[s for s in signoffs
            if not (s.get("evidence") or {}).get("product_id")
            and (not (s.get("evidence") or {}).get("cohort_id")
                 or (s.get("evidence") or {}).get("cohort_id")==cohort_id)]
        results={}
        for layer in LAYERS:
            layer_gates=[]
            latest_by_gate={}
            for item in cohort_signoffs:
                if item["release_layer"]==layer and item["gate_name"] not in latest_by_gate:
                    latest_by_gate[item["gate_name"]]=item
            reference=latest_by_gate.get("ENGINEERING") or next(iter(latest_by_gate.values()),None)
            for gate_name,signoff_key in GATES:
                missing=[]
                matching=[s for s in cohort_signoffs
                    if s["release_layer"]==layer and s["gate_name"]==signoff_key]
                latest=matching[0] if matching else None
                evidence=(latest or {}).get("evidence") or {}

                if gate_name=="ARCHITECTURE_RELEASE_INTEGRITY" and layer=="RELEASE_1_WORKFORCE":
                    if not migration_state.get("ok"):
                        missing.append("Live PostgreSQL connection and checksum-verified migrations 001–009")
                        if migration_state.get("missing"):
                            missing.append("Missing migration versions: "+", ".join(migration_state["missing"]))
                        if migration_state.get("drift"):
                            missing.append("Migration checksum/name drift: "+", ".join(migration_state["drift"]))

                if gate_name=="SECURITY_TENANT_ISOLATION":
                    missing.extend(_hard_security_missing())
                    if not _has_evidence(evidence,"tenant_isolation"):
                        missing.append("Verified tenant isolation and cross-tenant denial evidence")
                    if not _has_evidence(evidence,"scope_bound_hmac_v1"):
                        missing.append("Verified scope-bound HMAC v1 and 300-second replay protection")

                if gate_name=="BACKEND_INTEGRATION_READINESS" and layer=="RELEASE_1_WORKFORCE":
                    if not settings.database_url:
                        missing.append("Durable PostgreSQL repository configuration")

                if gate_name=="AI_WORKFORCE_ACCEPTANCE":
                    if not _has_evidence(evidence,"single_use_human_approval"):
                        missing.append("Single-use, payload-bound, separation-of-duties human approval evidence")
                    if not _has_evidence(evidence,"fail_closed_external_gateways"):
                        missing.append("Unconfigured external gateways fail-closed with audited HTTP 503")

                if gate_name=="RELIABILITY_DISASTER_RECOVERY":
                    if layer=="RELEASE_1_WORKFORCE":
                        if not any(h.get("is_fresh") for h in heartbeats):
                            missing.append("At least one worker heartbeat newer than 90 seconds")
                        if not _has_evidence(evidence,"backup_restore_test"):
                            missing.append("Verified database backup and restore test evidence")

                if gate_name=="BACKEND_INTEGRATION_READINESS" and layer=="RELEASE_2_PRODUCTS":
                    live=evidence.get("live_backend_verification") or {}
                    deferred=[]
                    unverified=[]
                    for pid in product_ids:
                        if pid in valid_deferrals:
                            deferred.append(pid)
                            continue
                        if pid=="growtoys":
                            # Discovery-only never becomes a production integration target.
                            deferred.append(pid)
                            continue
                        item=live.get(pid)
                        if (not isinstance(item,dict) or str(item.get("status","")).upper()!="PASS"
                                or not item.get("evidence_ref")):
                            unverified.append(pid)
                    unregistered=[pid for pid in product_ids if pid not in registered
                                  and pid not in valid_deferrals and pid!="growtoys"]
                    if unverified:
                        missing.append("Commit-bound live backend verification with evidence_ref for: "+", ".join(unverified))
                    if unregistered:
                        missing.append("Product adapters not registered: "+", ".join(unregistered))

                if gate_name=="PRODUCT_COMMERCIAL_READINESS" and layer=="RELEASE_2_PRODUCTS":
                    acceptance=evidence.get("product_acceptance") or {}
                    unaccepted=[]
                    for pid in product_ids:
                        if pid in valid_deferrals or pid=="growtoys":
                            continue
                        item=acceptance.get(pid)
                        if (not isinstance(item,dict) or str(item.get("status","")).upper()!="PASS"
                                or not item.get("evidence_ref")):
                            unaccepted.append(pid)
                    if unaccepted:
                        missing.append("Product-specific end-to-end acceptance with evidence_ref for: "+", ".join(unaccepted))

                if gate_name=="BACKEND_INTEGRATION_READINESS" and layer=="RELEASE_3_COMMERCIAL":
                    channels=evidence.get("live_communication_verification") or {}
                    for channel in ("whatsapp","email"):
                        item=channels.get(channel)
                        if (not isinstance(item,dict) or str(item.get("status","")).upper()!="PASS"
                                or not item.get("evidence_ref")):
                            missing.append("Live "+channel+" provider send/receipt verification with evidence_ref")

                if gate_name=="PRODUCT_COMMERCIAL_READINESS" and layer=="RELEASE_3_COMMERCIAL":
                    reconciliation=evidence.get("commercial_reconciliation") or {}
                    for item_name in ("revenue","customers","billing","consent","support"):
                        item=reconciliation.get(item_name)
                        if (not isinstance(item,dict) or str(item.get("status","")).upper()!="PASS"
                                or not item.get("evidence_ref")):
                            missing.append("Reconciled "+item_name+" evidence_ref")

                if latest and reference and (
                    latest.get("candidate_sha")!=reference.get("candidate_sha")
                    or latest.get("environment")!=reference.get("environment")
                ):
                    missing.append("Signoff candidate SHA/environment does not match the release layer's engineering baseline")
                if not latest:
                    missing.append(f"Explicit {signoff_key} signoff for {layer}")
                elif latest["status"]=="BLOCKED":
                    missing.append(f"Latest {signoff_key} signoff is BLOCKED")
                elif latest["status"]=="DEFERRED_WITH_ACCEPTANCE":
                    # Gate-wide deferrals are forbidden. Only product-scoped,
                    # validated RELEASE_2 product deferrals may be accepted.
                    missing.append("Gate-wide deferral is prohibited for shared release controls")

                if gate_name=="FINAL_RELEASE_AUTHORIZATION":
                    prior=[x for x in layer_gates if x["status"]!="PASS"]
                    if prior:
                        missing.append("All six preceding gates must PASS; product deferrals cannot waive shared gates")

                status="PASS" if latest and latest["status"]=="PASS" and not missing else "BLOCKED"
                layer_gates.append({"gate":gate_name,"signoff_gate":signoff_key,"status":status,
                    "missing_evidence":list(dict.fromkeys(missing)),"signoff":latest})

            results[layer]={"decision":"GO" if all(x["status"]=="PASS" for x in layer_gates) else "NO_GO",
                "candidate_sha":reference.get("candidate_sha") if reference else None,
                "environment":reference.get("environment") if reference else None,
                "gates":layer_gates}

        deferred_products=[]
        for pid,item in sorted(valid_deferrals.items()):
            if pid in product_ids:
                deferred_products.append({"product_id":pid,"status":"DEFERRED_WITH_ACCEPTANCE",
                    "owner":item["evidence"]["owner"],"due_date":item["evidence"]["due_date"],
                    "risk":item["evidence"]["accepted_risk"],"signoff":item})
        if cohort_id==PORTFOLIO_COHORT and "growtoys" in product_ids and "growtoys" not in valid_deferrals:
            deferred_products.append({"product_id":"growtoys","status":"DISCOVERY_ONLY",
                "note":"Live sales handoff and outreach remain blocked; HTTP 403."})

        return {"cohort_id":cohort_id,
            "cohort_product_ids":product_ids,
            "overall_decision":"GO" if all(v["decision"]=="GO" for v in results.values()) else "NO_GO",
            "release_layers":results,
            "deferred_products":deferred_products,
            "adapter_registry":adapters,
            "evidence_policy":"Only explicit product-scoped expansion deferrals may be accepted. Shared security, HMAC, human approval, reliability, and fail-closed gateway gates must PASS; missing, stale, unbound or incomplete evidence is BLOCKED."}
