"""Cohort-aware seven-gate evaluator; deferrals never waive security or reliability."""
from __future__ import annotations
import os
from .config import settings
LAYERS=("RELEASE_1_WORKFORCE","RELEASE_2_PRODUCTS","RELEASE_3_COMMERCIAL")
GATES=(("ARCHITECTURE_RELEASE_INTEGRITY","ENGINEERING"),("SECURITY_TENANT_ISOLATION","SECURITY"),("BACKEND_INTEGRATION_READINESS","INTEGRATION"),("AI_WORKFORCE_ACCEPTANCE","AI_GOVERNANCE"),("RELIABILITY_DISASTER_RECOVERY","RELIABILITY"),("PRODUCT_COMMERCIAL_READINESS","BUSINESS"),("FINAL_RELEASE_AUTHORIZATION","OPERATIONS"))
FLAGSHIP_PRODUCTS=("photo-studio-growth-os","clinics-counter")
DISCOVERY_ONLY_PRODUCTS={"growtoys"}
DEFERABLE_PRODUCTS={"photo-studio-billing","entrepreneur-instagram-tools","pharma-software-utilities","artists-business-tools","growtoys"}
def _has_evidence(evidence,key):
 value=(evidence or {}).get(key)
 return bool(value) and (not isinstance(value,dict) or all(bool(v) for v in value.values()))
def _valid_deferred(evidence):
 return isinstance(evidence,dict) and all(bool(evidence.get(k)) for k in ("accepted_risk","risk_rationale","owner","due_date","compensating_control"))
class ReleaseReadinessService:
 def __init__(self,repository,*,adapter_registry=None,now=None):
  self.repository=repository; self.adapter_registry=adapter_registry; self.now=now
 async def evaluate(self,cohort="portfolio"):
  if cohort not in {"portfolio","flagship"}: raise ValueError("Unsupported release cohort; use portfolio or flagship")
  all_signoffs=await self.repository.list_release_gate_signoffs()
  heartbeats=await self.repository.list_worker_heartbeats(stale_after_seconds=90)
  migration_state=await self.repository.verify_migration_chain()
  adapters=self.adapter_registry.readiness() if self.adapter_registry else []
  adapter_by_id={x["product_id"]:x for x in adapters}
  registered_ids=sorted(self.adapter_registry.product_ids) if self.adapter_registry else []
  deferred_rows=[s for s in all_signoffs if s.get("release_layer")=="RELEASE_2_PRODUCTS" and s.get("gate_name")=="BUSINESS" and s.get("status")=="DEFERRED_WITH_ACCEPTANCE" and s.get("cohort_id","portfolio")=="portfolio" and s.get("scope_key") not in {None,"cohort"}]
  deferred_by_product={}; invalid_deferred=[]
  for item in deferred_rows:
   pid=item.get("scope_key"); evidence=item.get("evidence") or {}
   expected_status="DISCOVERY_ONLY" if pid=="growtoys" else "NO_REPO_YET"
   if (not pid or pid not in DEFERABLE_PRODUCTS or not _valid_deferred(evidence)
       or evidence.get("product_id")!=pid or evidence.get("integration_status")!=expected_status):
    invalid_deferred.append(pid or "unknown")
   elif pid not in deferred_by_product:
    deferred_by_product[pid]=item
  product_ids=list(FLAGSHIP_PRODUCTS) if cohort=="flagship" else [pid for pid in registered_ids if pid not in DISCOVERY_ONLY_PRODUCTS and pid not in deferred_by_product]
  results={}
  for layer in LAYERS:
   layer_signoffs=[s for s in all_signoffs if s.get("release_layer")==layer and s.get("cohort_id","portfolio")==cohort and s.get("scope_key","cohort")=="cohort"]
   latest_by_gate={}
   for item in layer_signoffs:
    if item["gate_name"] not in latest_by_gate: latest_by_gate[item["gate_name"]]=item
   reference=latest_by_gate.get("ENGINEERING") or next(iter(latest_by_gate.values()),None)
   layer_gates=[]
   for gate_name,signoff_key in GATES:
    missing=[]; matching=[s for s in layer_signoffs if s["gate_name"]==signoff_key]; latest=matching[0] if matching else None
    evidence=(latest or {}).get("evidence") or {}
    if layer=="RELEASE_1_WORKFORCE":
     if gate_name=="ARCHITECTURE_RELEASE_INTEGRITY" and not migration_state.get("ok"):
      missing.append("Live PostgreSQL connection and checksum-verified migrations 001–009")
      if migration_state.get("missing"): missing.append("Missing migration versions: "+", ".join(migration_state["missing"]))
      if migration_state.get("drift"): missing.append("Migration checksum/name drift: "+", ".join(migration_state["drift"]))
     if gate_name=="SECURITY_TENANT_ISOLATION":
      if not (os.getenv("JWT_JWKS_URL") or settings.jwt_jwks_url or os.getenv("JWT_PUBLIC_KEY") or settings.jwt_public_key): missing.append("Production OIDC JWKS URL or asymmetric JWT public key")
      if len(os.getenv("STUDIO_GROWTH_OS_SERVICE_SECRET","").encode())<32: missing.append("32-byte minimum Studio Growth OS service HMAC secret")
      if len(os.getenv("RUNGROW_ADAPTER_WEBHOOK_SECRET","").encode())<32: missing.append("32-byte minimum adapter webhook HMAC secret")
     if gate_name=="BACKEND_INTEGRATION_READINESS" and not settings.database_url: missing.append("Durable PostgreSQL repository configuration")
     if gate_name=="AI_WORKFORCE_ACCEPTANCE" and not os.getenv("WORKFORCE_MODEL_PROVIDER"): missing.append("Explicit production AI model provider configuration")
     if gate_name=="RELIABILITY_DISASTER_RECOVERY":
      if not any(h.get("is_fresh") for h in heartbeats): missing.append("At least one worker heartbeat newer than 90 seconds")
      if not _has_evidence(evidence,"backup_restore_test"): missing.append("Verified database backup and restore test evidence")
     if gate_name=="PRODUCT_COMMERCIAL_READINESS" and not _has_evidence(evidence,"product_owner_acceptance"): missing.append("Release 1 product-owner acceptance evidence")
    elif layer=="RELEASE_2_PRODUCTS":
     if gate_name=="SECURITY_TENANT_ISOLATION":
      security=evidence.get("product_security_verification") or {}
      for pid in product_ids:
       item=security.get(pid); controls=("hmac_v1","tenant_isolation","single_use_approval","provider_fail_closed")
       if not isinstance(item,dict) or any(not isinstance(item.get(k),dict) or str(item[k].get("status","")).upper()!="PASS" or not item[k].get("evidence_ref") for k in controls):
        missing.append("Product HMAC, tenant isolation, single-use approval and fail-closed evidence for "+pid)
     if gate_name=="BACKEND_INTEGRATION_READINESS":
      live=evidence.get("live_backend_verification") or {}
      unverified=[pid for pid in product_ids if not isinstance(live.get(pid),dict) or str(live[pid].get("status","")).upper()!="PASS" or not live[pid].get("evidence_ref")]
      if unverified: missing.append("Commit-bound live backend verification with evidence_ref for: "+", ".join(unverified))
     if gate_name=="PRODUCT_COMMERCIAL_READINESS":
      acceptance=evidence.get("product_acceptance") or {}
      unaccepted=[pid for pid in product_ids if not isinstance(acceptance.get(pid),dict) or str(acceptance[pid].get("status","")).upper()!="PASS" or not acceptance[pid].get("evidence_ref")]
      if unaccepted: missing.append("Product-specific end-to-end acceptance with evidence_ref for: "+", ".join(unaccepted))
      if cohort=="portfolio" and invalid_deferred: missing.append("Invalid deferred-risk acceptance records: "+", ".join(invalid_deferred))
      if cohort=="portfolio":
       missing_discovery=[pid for pid in DISCOVERY_ONLY_PRODUCTS if pid in registered_ids and (pid not in deferred_by_product or adapter_by_id.get(pid,{}).get("readiness")!="DISCOVERY_ONLY")]
       if missing_discovery: missing.append("Discovery-only governance/deferred acceptance missing for: "+", ".join(sorted(missing_discovery)))
    elif layer=="RELEASE_3_COMMERCIAL":
     if gate_name=="BACKEND_INTEGRATION_READINESS":
      channels=evidence.get("live_communication_verification") or {}
      for channel in ("whatsapp","email"):
       item=channels.get(channel)
       if not isinstance(item,dict) or str(item.get("status","")).upper()!="PASS" or not item.get("evidence_ref"): missing.append("Live "+channel+" provider send/receipt verification with evidence_ref")
     if gate_name=="PRODUCT_COMMERCIAL_READINESS":
      reconciliation=evidence.get("commercial_reconciliation") or {}
      for item_name in ("revenue","customers","billing","consent","support"):
       item=reconciliation.get(item_name)
       if not isinstance(item,dict) or str(item.get("status","")).upper()!="PASS" or not item.get("evidence_ref"): missing.append("Reconciled "+item_name+" evidence_ref")
    if latest and reference and (latest.get("candidate_sha")!=reference.get("candidate_sha") or latest.get("environment")!=reference.get("environment")): missing.append("Signoff candidate SHA/environment does not match this cohort's engineering baseline")
    if not latest: missing.append(f"Explicit {signoff_key} signoff for {layer} cohort={cohort}")
    elif latest["status"]=="BLOCKED": missing.append(f"Latest {signoff_key} signoff is BLOCKED")
    elif latest["status"]=="DEFERRED_WITH_ACCEPTANCE":
     if layer!="RELEASE_2_PRODUCTS" or gate_name!="PRODUCT_COMMERCIAL_READINESS": missing.append("This gate is non-deferrable; accepted risk cannot bypass security, reliability or control evidence")
     elif not _valid_deferred(evidence): missing.append("Deferred acceptance requires accepted_risk, risk_rationale, owner, due_date and compensating_control")
    if gate_name=="FINAL_RELEASE_AUTHORIZATION":
     prior=[x for x in layer_gates if x["status"]!="PASS"]
     if prior: missing.append("All six preceding gates must PASS; a deferred product scope cannot waive a control gate")
    status="PASS" if latest and latest["status"]=="PASS" and not missing else "BLOCKED"
    if latest and latest["status"]=="DEFERRED_WITH_ACCEPTANCE" and not missing: status="DEFERRED_WITH_ACCEPTANCE"
    layer_gates.append({"gate":gate_name,"signoff_gate":signoff_key,"status":status,"missing_evidence":list(dict.fromkeys(missing)),"signoff":latest})
   results[layer]={"decision":"GO" if all(x["status"]=="PASS" for x in layer_gates) else "NO_GO","candidate_sha":reference.get("candidate_sha") if reference else None,"environment":reference.get("environment") if reference else None,"gates":layer_gates}
  return {"cohort":cohort,"overall_decision":"GO" if all(v["decision"]=="GO" for v in results.values()) else "NO_GO","release_layers":results,"product_scope":{"required_live_products":product_ids,"deferred_products":sorted(deferred_by_product),"invalid_deferred_products":sorted(invalid_deferred),"discovery_only_products":sorted(DISCOVERY_ONLY_PRODUCTS & set(registered_ids))},"gate_definitions":[{"gate":a,"signoff_gate":b} for a,b in GATES],"evidence_policy":"Deferrals apply only to product-scoped Release 2 business scope. Security, tenant isolation, HMAC, single-use approval, fail-closed provider and reliability controls are never deferrable."}
