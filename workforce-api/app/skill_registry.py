"""Versioned runtime SOP and product-playbook injection for RunGrow supervisors."""
from __future__ import annotations
import hashlib, json
from copy import deepcopy

DEPARTMENT_SOPS = {
 "Growth & Marketing": {"version":"growth-sop-2.2.0","instructions":["Use allowlisted public sources only; retain source URLs and evidence snippets.","Score fit against the selected product ICP; distinguish facts from hypotheses.","Never publish, launch campaigns, or spend budget without a separate human approval."],"acceptance":["At least one attributable source or an explicit blocked/no-evidence result","ICP fit and confidence are explicit","No outbound action or spend is authorised by research output"],"compliance":["robots.txt and domain allowlist are fail-closed","No sensitive personal profiling"]},
 "Sales & Revenue": {"version":"sales-sop-2.2.0","instructions":["Qualify accounts using evidence, product fit, city and specialty.","Outreach must be personalised, respectful, and clearly disclose AI identity.","Never send messages or make contractual commitments without single-use approval."],"acceptance":["Account evidence and ICP score are present","Draft is channel-specific and includes AI identity disclosure","No external message was sent"],"compliance":["Indian business English","No fabricated claims, discounts, or customer references"]},
 "Customer Success": {"version":"cs-sop-2.2.0","instructions":["Prepare a product-specific onboarding plan with milestones and ownership.","Validate tenant and product binding before creating an adapter payload.","Provisioning remains pending until human approval and verified tenant binding."],"acceptance":["Product and tenant scope are explicit","Payload is versioned and signature-ready","No customer tenant was provisioned"],"compliance":["Least privilege","No cross-tenant data access"]},
 "Finance & Administration": {"version":"finance-sop-2.2.0","instructions":["Quote only published product tiers; calculate line items in INR.","Show assumptions, taxes as excluded unless verified, and ROI inputs separately.","Never initiate payment or approve spend."],"acceptance":["INR currency and itemised line items","Totals reconcile arithmetically","Spend and payment are not authorised"],"compliance":["No invented pricing; fail closed if price book is absent","Human approval for financial commitments"]},
 "Legal, Risk & Compliance": {"version":"legal-sop-2.2.0","instructions":["Check AI disclosure, privacy, consent, claims and jurisdictional risks.","Separate legal facts from review questions; escalate uncertainty.","Do not represent automated checks as legal advice or approval."],"acceptance":["AI disclosure present for outbound drafts","Risks and missing evidence are explicit","Publication remains unauthorised"],"compliance":["Human legal review for contracts and regulated claims"]},
 "Product & Design": {"version":"product-sop-2.2.0","instructions":["Map requests to verified product capabilities and documented constraints.","Separate discovery hypotheses from committed roadmap items."],"acceptance":["Requirements trace to stated user objective","Unknown capabilities are labelled for validation"],"compliance":["No unsupported product promises"]},
 "Engineering & IT": {"version":"engineering-sop-2.2.0","instructions":["Preserve tenant isolation, least privilege, auditability and backward compatibility.","Require tests for behavior changes; never deploy directly from an agent task."],"acceptance":["Security and tenant boundaries are explicit","Tests and rollback considerations are documented"],"compliance":["No production mutation or deployment without approval"]},
}
PRODUCT_PLAYBOOKS = {
 "photo-studio-growth-os":{"version":"psgo-playbook-2.2.1","stage":"COMMERCIAL","tiers":{"Starter":{"monthly_inr":4999},"Pro":{"monthly_inr":9999},"Studio Enterprise":{"monthly_inr":19999}},"pricing_status":"PROVISIONAL_FOUNDER_APPROVED","icp":"Photography studios, wedding photographers and creative businesses"},
 "clinics-counter":{"version":"clinics-counter-playbook-2.2.1","stage":"COMMERCIAL","tiers":{"Clinic Starter":{"monthly_inr":3999},"Multi-Chair Pro":{"monthly_inr":8999}},"pricing_status":"PROVISIONAL_FOUNDER_APPROVED","icp":"Clinics and small healthcare practices; avoid patient-level data in prospecting"},
 "growtoys":{"version":"growtoys-playbook-2.2.0","stage":"DISCOVERY_ONLY","tiers":{},"icp":"Discovery interviews only; no commercial claims or live conversion"},
}
ROLE_OVERRIDES = {
 "market-intel":["Research and cite public account evidence."],
 "lead-research":["Validate prospect identity and ICP evidence."],
 "outbound-sdr":["Draft only; never send outreach."],
 "ae":["Prepare proposals for human review; no negotiation."],
 "onboarding":["Prepare a provisioning payload; never provision a tenant."],
 "finance":["Reconcile itemised INR pricing and disclose assumptions."],
 "legal":["Escalate legal uncertainty to qualified human counsel."],
}
def resolve_runtime_skill_bundle(department: str, product_id: str | None, role_id: str) -> dict:
    if department not in DEPARTMENT_SOPS:
        raise ValueError(f"Unknown department SOP: {department}")
    if product_id and product_id not in PRODUCT_PLAYBOOKS:
        raise ValueError(f"Unknown product playbook: {product_id}")
    sop=deepcopy(DEPARTMENT_SOPS[department])
    playbook=deepcopy(PRODUCT_PLAYBOOKS.get(product_id,{}))
    if playbook.get("stage")=="DISCOVERY_ONLY":
        sop["instructions"] += ["DISCOVERY_ONLY HARD LOCK: research and internal discovery only; block live outreach, proposals, commercial pricing and onboarding/provisioning."]
        sop["acceptance"] += ["No external outreach, quote, activation, or tenant provisioning"]
        sop["compliance"] += ["DISCOVERY_ONLY enforcement is mandatory and non-overridable"]
    sop["instructions"] += ROLE_OVERRIDES.get(role_id,[])
    payload={"department":department,"product_id":product_id,"role_id":role_id,"sop":sop,"playbook":playbook}
    version=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    return {"department":department,"product_id":product_id,"role_id":role_id,
            "instructions":tuple(sop["instructions"]),"acceptance_criteria":tuple(sop["acceptance"]),
            "compliance_rules":tuple(sop["compliance"]),"product_playbook":playbook,
            "skill_version_hash":version,"skill_version":sop["version"]}
