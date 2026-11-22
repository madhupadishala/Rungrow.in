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
 "photo-studio-billing":{"version":"psb-playbook-2.2.0","stage":"COMMERCIAL","tiers":{},"icp":"Photography studios, photo labs and printing businesses"},
 "entrepreneur-instagram-tools":{"version":"entrepreneur-playbook-2.2.0","stage":"COMMERCIAL","tiers":{},"icp":"Indian SMB owners, creators and Instagram sellers"},
 "pharma-software-utilities":{"version":"pharma-playbook-2.2.0","stage":"COMMERCIAL","tiers":{},"icp":"Pharma businesses and pharmacovigilance teams"},
 "artists-business-tools":{"version":"artists-playbook-2.2.0","stage":"COMMERCIAL","tiers":{},"icp":"Artists, performers and creative professionals"},
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


# Fortune-50 composable operating skills. Each definition is independently versioned,
# rubric-backed and promoted only by the isolated SkillGymEngine.
_SKILL_ROLE_MAP = {
    "social-media-lead": ("Growth & Marketing", ["brand positioning","viral hook design","editorial calendar","channel strategy","social listening","audience segmentation","content repurposing","creator collaboration","social attribution","brand safety"]),
    "social-engagement": ("Growth & Marketing", ["comment triage","DM intent classification","lead qualification","objection response","demo handoff","quotation routing","community moderation","response empathy","SLA prioritisation","escalation writing"]),
    "social-events": ("Growth & Marketing", ["webinar strategy","launch run-of-show","SMB workshop planning","speaker briefing","registration funnel","event promotion","demo-day operations","attendee segmentation","post-event nurture","event ROI reporting"]),
    "social-publisher": ("Growth & Marketing", ["LinkedIn native copy","Instagram carousel brief","Instagram reel brief","X thread drafting","YouTube Shorts scripting","UTM tagging","AI disclosure QA","claims compliance","publishing queue design","distribution analytics"]),
    "community-manager": ("Growth & Marketing", ["WhatsApp community charter","Telegram community charter","peer Q&A facilitation","member onboarding","trust moderation","retention prompts","community sentiment","customer escalation","community event cadence","privacy-safe engagement"]),
    "customer-advocacy": ("Customer Success", ["ROI interview","case study evidence","customer consent capture","review request drafting","referral loop design","outcome verification","testimonial QA","advocacy segmentation","renewal advocacy","social proof handoff"]),
    "growth-director": ("Growth & Marketing", ["growth experiment design","funnel diagnosis","channel mix planning","campaign KPI definition","budget scenario modelling","ICP messaging","conversion optimisation","attribution governance","quarterly growth plan","campaign retrospective"]),
    "brand": ("Growth & Marketing", ["brand voice guide","problem-first storytelling","value proposition","proof-point validation","creative brief","content quality rubric","regulated claim review","brand consistency","founder narrative","editorial QA"]),
    "campaigns": ("Growth & Marketing", ["campaign architecture","80-20 content mix","multichannel calendar","UTM taxonomy","launch checklist","audience cohort design","organic distribution","paid campaign brief","campaign measurement","campaign postmortem"]),
    "inbound-sdr": ("Sales & Revenue", ["inbound qualification","discovery question design","buying intent scoring","pricing objection handling","demo booking draft","quotation brief","follow-up sequencing","lead response SLA","need summarisation","handoff to account executive"]),
    "ae": ("Sales & Revenue", ["consultative discovery","INR proposal drafting","ROI case framing","commercial objection handling","demo narrative","decision mapping","stakeholder follow-up","negotiation preparation","proposal QA","close plan"]),
    "renewals": ("Customer Success", ["adoption health review","churn risk detection","value realisation review","renewal readiness","expansion signal capture","customer QBR","retention campaign","customer feedback loop","at-risk recovery plan","renewal handoff"]),
}
def _skill_id(role_id: str, index: int) -> str:
    return f"{role_id}.{index + 1:02d}"
def _skill_instructions(role_id: str, skill_name: str) -> str:
    return (
        f"Act as the {role_id} specialist executing {skill_name}. Start with the Indian SMB customer's "
        "operational problem, give practical and specific value, use only verified product capabilities "
        "and evidence, disclose AI identity in external-facing drafts, avoid fabricated results, and end "
        "with one low-friction next step. Draft only; never publish, send, spend or commit."
    )
def _skill_rubric() -> dict:
    return {"clarity":0.20,"problem_first_value":0.20,"indian_smb_relevance":0.20,
            "ai_disclosure_compliance":0.25,"conversion_cta":0.15,
            "minimum_overall_score":0.85}
FORTUNE50_SKILLS = tuple(
    {"skill_id":_skill_id(role_id,index),"department":department,"role_id":role_id,
     "category":("sales" if department=="Sales & Revenue" else "retention" if role_id in {"customer-advocacy","renewals"} else "digital_media"),
     "skill_name":name,"instructions":_skill_instructions(role_id,name),
     "rubric_json":_skill_rubric(),"status":"DRAFT"}
    for role_id,(department,names) in _SKILL_ROLE_MAP.items()
    for index,name in enumerate(names)
)
if len(FORTUNE50_SKILLS) < 100 or len({item["skill_id"] for item in FORTUNE50_SKILLS}) != len(FORTUNE50_SKILLS):
    raise RuntimeError("Fortune-50 skill catalog must contain 100+ unique composable skills")
SKILLS_BY_ID = {item["skill_id"]:item for item in FORTUNE50_SKILLS}
