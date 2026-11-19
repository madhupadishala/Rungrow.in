"""Evidence-driven seven-gate evaluator; absence of evidence is never a PASS."""
from __future__ import annotations
import os
from .config import settings
from .migrate import migration_files

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
class ReleaseReadinessService:
    def __init__(self,repository,*,adapter_registry=None,now=None):
        self.repository=repository; self.adapter_registry=adapter_registry
        self.now=now
    async def evaluate(self):
        signoffs=await self.repository.list_release_gate_signoffs()
        heartbeats=await self.repository.list_worker_heartbeats()
        expected={v for v,_,_ in migration_files()}
        migration_state=await self.repository.verify_migration_chain()
        migration_ok=bool(migration_state.get("ok"))
        adapters=self.adapter_registry.readiness() if self.adapter_registry else []
        results={}
        for layer in LAYERS:
            layer_gates=[]
            for gate_name,signoff_key in GATES:
                evidence=[]
                matching=[s for s in signoffs if s["release_layer"]==layer and s["gate_name"]==signoff_key]
                latest=matching[0] if matching else None
                if layer=="RELEASE_1_WORKFORCE":
                    if gate_name=="ARCHITECTURE_RELEASE_INTEGRITY" and not migration_ok:
                        evidence.append("DATABASE_URL and checksum-verified migration chain 001–008")
                    if gate_name=="SECURITY_TENANT_ISOLATION" and not (
                        os.getenv("JWT_JWKS_URL") or settings.jwt_jwks_url or os.getenv("JWT_PUBLIC_KEY") or settings.jwt_public_key):
                        evidence.append("Production OIDC JWKS URL or asymmetric JWT public key")
                    if gate_name=="BACKEND_INTEGRATION_READINESS" and not settings.database_url:
                        evidence.append("Durable PostgreSQL repository connectivity")
                    if gate_name=="AI_WORKFORCE_ACCEPTANCE" and not os.getenv("STUDIO_GROWTH_OS_SERVICE_SECRET"):
                        evidence.append("Configured service identity and governed integration secret")
                    if gate_name=="RELIABILITY_DISASTER_RECOVERY":
                        fresh=[h for h in heartbeats if h.get("is_fresh")]
                        if not fresh: evidence.append("At least one worker heartbeat newer than 90 seconds")
                    if gate_name=="PRODUCT_COMMERCIAL_READINESS":
                        evidence.append("Product owner acceptance evidence for Release 1")
                    if gate_name=="FINAL_RELEASE_AUTHORIZATION" and not any(
                        s["release_layer"]==layer and s["gate_name"]=="OPERATIONS" and s["status"]=="PASS" for s in signoffs):
                        evidence.append("Release 1 operations authorization signoff")
                elif layer=="RELEASE_2_PRODUCTS":
                    if gate_name=="BACKEND_INTEGRATION_READINESS":
                        ready=[a for a in adapters if str(a.get("readiness","")).upper()=="READY"]
                        if len(ready)<7: evidence.append("Verified live backend connectivity for all seven product adapters (not registry-only readiness)")
                    if gate_name=="PRODUCT_COMMERCIAL_READINESS":
                        evidence.append("Product-specific end-to-end acceptance and customer workflow evidence for each enabled vertical")
                elif layer=="RELEASE_3_COMMERCIAL":
                    if gate_name=="PRODUCT_COMMERCIAL_READINESS":
                        evidence.append("Reconciled revenue, customer, billing, consent and commercial operations evidence")
                    if gate_name=="BACKEND_INTEGRATION_READINESS":
                        evidence.append("Live WhatsApp and transactional email provider verification")
                if latest and latest["status"]=="PASS" and not evidence:
                    status="PASS"
                elif latest and latest["status"]=="DEFERRED_WITH_ACCEPTANCE" and not evidence:
                    status="DEFERRED_WITH_ACCEPTANCE"
                else:
                    status="BLOCKED"
                    if not latest: evidence.append(f"Explicit {signoff_key} signoff for {layer}")
                    elif latest["status"]!="PASS": evidence.append(f"Latest {signoff_key} signoff is {latest['status']}")
                layer_gates.append({"gate":gate_name,"signoff_gate":signoff_key,"status":status,
                    "missing_evidence":list(dict.fromkeys(evidence)),"signoff":latest})
            results[layer]={"decision":"GO" if all(x["status"] in {"PASS","DEFERRED_WITH_ACCEPTANCE"} for x in layer_gates) else "NO_GO",
                "gates":layer_gates}
        overall="GO" if all(v["decision"]=="GO" for v in results.values()) else "NO_GO"
        return {"overall_decision":overall,"release_layers":results,
            "gate_definitions":[{"gate":a,"signoff_gate":b} for a,b in GATES],
            "evidence_policy":"Missing or stale evidence is BLOCKED; no readiness is inferred from configuration alone."}
