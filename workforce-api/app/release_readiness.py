"""Evidence-driven seven-gate evaluator; absence of evidence is never a PASS."""
from __future__ import annotations
import os
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

def _has_evidence(evidence, key):
    value=(evidence or {}).get(key)
    return bool(value) and (not isinstance(value,dict) or all(bool(v) for v in value.values()))

class ReleaseReadinessService:
    def __init__(self,repository,*,adapter_registry=None,now=None):
        self.repository=repository
        self.adapter_registry=adapter_registry
        self.now=now

    async def evaluate(self):
        signoffs=await self.repository.list_release_gate_signoffs()
        heartbeats=await self.repository.list_worker_heartbeats(stale_after_seconds=90)
        migration_state=await self.repository.verify_migration_chain()
        adapters=self.adapter_registry.readiness() if self.adapter_registry else []
        product_ids=sorted(self.adapter_registry.product_ids) if self.adapter_registry else []
        results={}
        for layer in LAYERS:
            layer_gates=[]
            for index,(gate_name,signoff_key) in enumerate(GATES):
                missing=[]
                matching=[s for s in signoffs if s["release_layer"]==layer and s["gate_name"]==signoff_key]
                latest=matching[0] if matching else None
                evidence=(latest or {}).get("evidence") or {}

                if layer=="RELEASE_1_WORKFORCE":
                    if gate_name=="ARCHITECTURE_RELEASE_INTEGRITY" and not migration_state.get("ok"):
                        missing.append("Live PostgreSQL connection and checksum-verified migrations 001–008")
                        if migration_state.get("missing"): missing.append("Missing migration versions: "+", ".join(migration_state["missing"]))
                        if migration_state.get("drift"): missing.append("Migration checksum/name drift: "+", ".join(migration_state["drift"]))
                    if gate_name=="SECURITY_TENANT_ISOLATION":
                        if not (os.getenv("JWT_JWKS_URL") or settings.jwt_jwks_url or os.getenv("JWT_PUBLIC_KEY") or settings.jwt_public_key):
                            missing.append("Production OIDC JWKS URL or asymmetric JWT public key")
                        if len(os.getenv("STUDIO_GROWTH_OS_SERVICE_SECRET","").encode())<32:
                            missing.append("32-byte minimum Studio Growth OS service HMAC secret")
                        if len(os.getenv("RUNGROW_ADAPTER_WEBHOOK_SECRET","").encode())<32:
                            missing.append("32-byte minimum adapter webhook HMAC secret")
                    if gate_name=="BACKEND_INTEGRATION_READINESS" and not settings.database_url:
                        missing.append("Durable PostgreSQL repository configuration")
                    if gate_name=="AI_WORKFORCE_ACCEPTANCE" and not os.getenv("WORKFORCE_MODEL_PROVIDER"):
                        missing.append("Explicit production AI model provider configuration")
                    if gate_name=="RELIABILITY_DISASTER_RECOVERY":
                        if not any(h.get("is_fresh") for h in heartbeats):
                            missing.append("At least one worker heartbeat newer than 90 seconds")
                        if not _has_evidence(evidence,"backup_restore_test"):
                            missing.append("Verified database backup and restore test evidence")
                    if gate_name=="PRODUCT_COMMERCIAL_READINESS" and not _has_evidence(evidence,"product_owner_acceptance"):
                        missing.append("Release 1 product-owner acceptance evidence")
                elif layer=="RELEASE_2_PRODUCTS":
                    if gate_name=="BACKEND_INTEGRATION_READINESS":
                        live=evidence.get("live_backend_verification") or {}
                        unverified=[pid for pid in product_ids
                            if not isinstance(live.get(pid),dict)
                            or str(live[pid].get("status","")).upper()!="PASS"
                            or not live[pid].get("evidence_ref")]
                        if unverified:
                            missing.append("Commit-bound live backend verification with evidence_ref for: "+", ".join(unverified))
                    if gate_name=="PRODUCT_COMMERCIAL_READINESS":
                        acceptance=evidence.get("product_acceptance") or {}
                        unaccepted=[pid for pid in product_ids
                            if not isinstance(acceptance.get(pid),dict)
                            or str(acceptance[pid].get("status","")).upper()!="PASS"
                            or not acceptance[pid].get("evidence_ref")]
                        if unaccepted:
                            missing.append("Product-specific end-to-end acceptance with evidence_ref for: "+", ".join(unaccepted))
                elif layer=="RELEASE_3_COMMERCIAL":
                    if gate_name=="BACKEND_INTEGRATION_READINESS":
                        channels=evidence.get("live_communication_verification") or {}
                        for channel in ("whatsapp","email"):
                            item=channels.get(channel)
                            if not isinstance(item,dict) or str(item.get("status","")).upper()!="PASS" or not item.get("evidence_ref"):
                                missing.append("Live "+channel+" provider send/receipt verification with evidence_ref")
                    if gate_name=="PRODUCT_COMMERCIAL_READINESS":
                        required=("revenue","customers","billing","consent","support")
                        reconciliation=evidence.get("commercial_reconciliation") or {}
                        for item in required:
                            value=reconciliation.get(item)
                            if not isinstance(value,dict) or str(value.get("status","")).upper()!="PASS" or not value.get("evidence_ref"):
                                missing.append("Reconciled "+item+" evidence_ref")

                if not latest:
                    missing.append(f"Explicit {signoff_key} signoff for {layer}")
                elif latest["status"]=="BLOCKED":
                    missing.append(f"Latest {signoff_key} signoff is BLOCKED")
                elif latest["status"]=="DEFERRED_WITH_ACCEPTANCE" and not evidence.get("accepted_risk"):
                    missing.append("Deferred gate requires accepted_risk, owner, due_date and compensating_control in signoff evidence")
                if gate_name=="FINAL_RELEASE_AUTHORIZATION":
                    prior=[x for x in layer_gates if x["status"] not in {"PASS","DEFERRED_WITH_ACCEPTANCE"}]
                    if prior:
                        missing.append("All six preceding gates must pass before final release authorization")
                if latest and latest["status"]=="PASS" and not missing:
                    status="PASS"
                elif latest and latest["status"]=="DEFERRED_WITH_ACCEPTANCE" and not missing:
                    status="DEFERRED_WITH_ACCEPTANCE"
                else:
                    status="BLOCKED"
                layer_gates.append({"gate":gate_name,"signoff_gate":signoff_key,"status":status,
                    "missing_evidence":list(dict.fromkeys(missing)),"signoff":latest})
            results[layer]={"decision":"GO" if all(x["status"] in {"PASS","DEFERRED_WITH_ACCEPTANCE"} for x in layer_gates) else "NO_GO",
                "gates":layer_gates}
        overall="GO" if all(v["decision"]=="GO" for v in results.values()) else "NO_GO"
        return {"overall_decision":overall,"release_layers":results,
            "gate_definitions":[{"gate":a,"signoff_gate":b} for a,b in GATES],
            "evidence_policy":"Missing, stale, unbound or incomplete evidence is BLOCKED; adapter registration and configuration alone never prove live connectivity."}
