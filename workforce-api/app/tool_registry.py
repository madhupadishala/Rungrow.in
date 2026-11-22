"""Permission-aware specialist tools for Workforce 2.1.

All tools are deterministic, evidence-producing and side-effect free. External
publication, outreach and spend are deliberately not registered as executable tools.
"""
from __future__ import annotations
import hashlib, json, re, time, os
import httpx
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse
from .registry import BY_ID, Employee
from .commercial_intelligence import RobotsCompliantHttpFetcher, TinyFishResearchProvider, score_icp, AI_IDENTITY_DISCLOSURE
from .skill_registry import PRODUCT_PLAYBOOKS

@dataclass(frozen=True)
class SpecialistTool:
    name: str
    roles: frozenset[str]
    required_permissions: frozenset[str]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    external_effect: bool = False

class ToolAuthorizationError(PermissionError): pass

class SearXNGDirectorySearch:
    """Optional self-hosted SearXNG JSON search; results are constrained to approved domains."""
    def __init__(self, base_url=None, allow_domains=None):
        self.base_url=(base_url or os.getenv("RUNGROW_SEARXNG_URL","")).rstrip("/")
        self.allow_domains=set(allow_domains or (x.strip().lower() for x in os.getenv("RUNGROW_RESEARCH_ALLOW_DOMAINS","").split(",") if x.strip()))

    async def search(self, query, city, limit=20):
        if not self.base_url or not self.allow_domains:
            return []
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
            response=await client.get(self.base_url+"/search",params={"q":f"{query} {city} business directory","format":"json"})
            response.raise_for_status()
            data=response.json()
        urls=[]
        for item in data.get("results",[])[:max(1,min(limit,50))]:
            url=str(item.get("url",""))
            host=urlparse(url).hostname or ""
            host=host.lower().rstrip(".")
            if any(host==domain or host.endswith("."+domain) for domain in self.allow_domains):
                urls.append(url)
        return urls


class WorkforceToolRegistry:
    def __init__(self, directory_fetcher=None):
        allow_domains={x.strip().lower().lstrip('.') for x in os.getenv('RUNGROW_RESEARCH_ALLOW_DOMAINS','').split(',') if x.strip()}
        deny_domains={x.strip().lower().lstrip('.') for x in os.getenv('RUNGROW_RESEARCH_DENY_DOMAINS','').split(',') if x.strip()}
        if directory_fetcher is not None:
            self.directory_fetcher=directory_fetcher
        elif os.getenv('TINYFISH_API_KEY','').strip():
            self.directory_fetcher=TinyFishResearchProvider(api_key=os.getenv('TINYFISH_API_KEY'),allow_domains=allow_domains,deny_domains=deny_domains)
        else:
            self.directory_fetcher=RobotsCompliantHttpFetcher(allow_domains=allow_domains,deny_domains=deny_domains)
        self.directory_search = SearXNGDirectorySearch(allow_domains=self.directory_fetcher.allow_domains)
        self._tools: dict[str, SpecialistTool] = {}
        self.register(SpecialistTool("public_directory_research",frozenset({"market-intel","lead-research"}),
            frozenset({"research.public"}),self._research))
        self.register(SpecialistTool("icp_lead_scoring",frozenset({"market-intel","lead-research","sales-ops"}),
            frozenset({"lead.enrich"}),self._score))
        self.register(SpecialistTool("campaign_budget_estimate",frozenset({"growth-director","campaigns","finance"}),
            frozenset({"finance.read"}),self._budget))
        self.register(SpecialistTool("compliance_disclosure_check",frozenset({"legal","brand","campaigns"}),
            frozenset({"compliance.read"}),self._compliance))
        self.register(SpecialistTool("product_handoff_draft",frozenset({"sales-ops","ae","cs-director","onboarding"}),
            frozenset({"customer.read"}),self._handoff))
        self.register(SpecialistTool("account_research_brief",frozenset({"market-intel","lead-research","outbound-sdr"}),
            frozenset({"research.public"}),self._account_research))
        self.register(SpecialistTool("personalized_outreach_draft",frozenset({"outbound-sdr"}),
            frozenset({"message.draft"}),self._outreach))
        self.register(SpecialistTool("commercial_proposal_builder",frozenset({"ae","solutions"}),
            frozenset({"proposal.draft"}),self._proposal))
        self.register(SpecialistTool("product_onboarding_trigger",frozenset({"onboarding","sales-ops"}),
            frozenset({"customer.update"}),self._onboarding))
    def register(self, tool: SpecialistTool):
        if tool.name in self._tools: raise ValueError(f"Duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def names(self): return tuple(sorted(self._tools))

    def execute(self, *, name: str, employee: Employee, principal_permissions: frozenset[str],
                tenant_id: str, payload: dict[str, Any], approved_external: bool=False) -> tuple[dict[str, Any], int, str]:
        tool=self._tools.get(name)
        if tool is None: raise KeyError(f"Unknown specialist tool: {name}")
        if employee.id not in tool.roles: raise ToolAuthorizationError("Role is not authorised for this tool")
        if not tool.required_permissions.issubset(set(employee.permissions)):
            raise ToolAuthorizationError("Registered employee permissions do not grant this tool")
        if "*" not in principal_permissions and not ({"agent.run","work.assign"} & set(principal_permissions)):
            raise ToolAuthorizationError("Orchestration principal lacks agent execution authority")
        if tool.external_effect and not approved_external:
            raise ToolAuthorizationError("External action requires single-use human approval")
        started=time.perf_counter()
        result=tool.handler(payload)
        latency=max(0,int((time.perf_counter()-started)*1000))
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
        return result,latency,digest

    def _research(self,p):
        urls=p.get("source_urls",[])
        if not isinstance(urls,list) or len(urls)>30: raise ValueError("source_urls must be a list of at most 30 URLs")
        # Sync worker uses a dedicated async fetch bridge in orchestration.py.
        return {"requested_urls":urls,"query":p.get("query",""),"city":p.get("city",""),
                "status":"FETCH_REQUIRED","robots_policy":"fail_closed","allowlist_enforced":True}

    def _score(self,p):
        product=str(p.get("product_id",""))
        candidates=p.get("candidates",[])
        if not isinstance(candidates,list): raise ValueError("candidates must be an array")
        return {"scores":[{"business_name":c.get("business_name",""),**score_icp(product,c)} for c in candidates],
                "scoring":"deterministic_product_playbook","evidence_required":True}

    def _budget(self,p):
        count=max(0,int(p.get("lead_count",0)))
        cost=max(0.0,float(p.get("cost_per_lead_inr",0)))
        contingency=min(0.3,max(0.0,float(p.get("contingency_pct",0.1))))
        return {"currency":"INR","lead_count":count,"base_budget_inr":round(count*cost,2),
                "contingency_pct":contingency,"recommended_budget_inr":round(count*cost*(1+contingency),2),
                "spend_authorised":False}

    def _compliance(self,p):
        copy=str(p.get("draft",""))
        missing=[]
        if not re.search(r"\bAI\b|artificial intelligence",copy,re.I): missing.append("AI identity disclosure")
        if not re.search(r"\bRunGrow\b",copy): missing.append("organisation identity")
        return {"passed":not missing,"missing_requirements":missing,
                "disclosure_standard":AI_IDENTITY_DISCLOSURE,"publication_authorised":False}

    def _handoff(self,p):
        return {"product_id":p.get("product_id"),"prospect":p.get("prospect",{}),
                "sales_context":p.get("sales_context",{}),"recommended_next_step":p.get("next_step","human discovery"),
                "external_contact_performed":False,"source_evidence":p.get("source_evidence",[])}

    @staticmethod
    def _assert_commercial_product(product_id, live_action=False):
        playbook=PRODUCT_PLAYBOOKS.get(str(product_id))
        if not playbook: raise ValueError("Unknown product playbook")
        if live_action and playbook.get("stage")=="DISCOVERY_ONLY":
            raise ToolAuthorizationError("DISCOVERY_ONLY product: live commercial action blocked")
        return playbook

    def _account_research(self,p):
        playbook=self._assert_commercial_product(p.get("product_id"))
        candidates=p.get("candidates",[])
        if not isinstance(candidates,list): raise ValueError("candidates must be an array")
        evidence=[]
        for item in candidates[:20]:
            url=str(item.get("source_url",""))
            if url.startswith("https://") and urlparse(url).hostname:
                evidence.append({"business_name":item.get("business_name",""),"city":item.get("city",p.get("city","")),
                    "specialty":item.get("segment",""),"source_url":url,"evidence_snippet":item.get("evidence_snippet","")})
        return {"product_id":p.get("product_id"),"account_profile":{"business_name":p.get("business_name") or (evidence[0]["business_name"] if evidence else ""),
            "city":p.get("city",""),"specialty":p.get("specialty",""),"digital_maturity":p.get("digital_maturity","unknown"),
            "pain_points":p.get("pain_points",[]),"decision_maker_hooks":p.get("decision_maker_hooks",[])},
            "evidence":evidence,"icp_definition":playbook["icp"],"evidence_status":"VERIFIED" if evidence else "INSUFFICIENT",
            "external_contact_performed":False}

    def _outreach(self,p):
        self._assert_commercial_product(p.get("product_id"),live_action=True)
        channel=str(p.get("channel","whatsapp")).lower()
        if channel not in {"whatsapp","email"}: raise ValueError("channel must be whatsapp or email")
        disclosure=AI_IDENTITY_DISCLOSURE
        body=str(p.get("body","")).strip()
        if "ai" not in body.lower() and "artificial intelligence" not in body.lower(): body += "\\n\\n"+disclosure
        return {"channel":channel,"recipient_ref":p.get("prospect_ref"),"draft":body,
            "ai_identity_disclosure":disclosure,"approval_status":"PENDING_HUMAN_APPROVAL",
            "approval_required":True,"queued":True,"sent":False}

    def _proposal(self,p):
        playbook=self._assert_commercial_product(p.get("product_id"),live_action=True)
        tier=str(p.get("tier","Starter"))
        if tier not in playbook["tiers"]: raise ValueError("Tier is not in published price book")
        price=float(playbook["tiers"][tier]["monthly_inr"])
        months=max(1,min(36,int(p.get("months",12))))
        total=round(price*months,2)
        return {"product_id":p["product_id"],"currency":"INR","tier":tier,
            "line_items":[{"description":tier+" subscription","quantity":months,"unit_price_inr":price,"total_inr":total}],
            "subtotal_inr":total,"taxes":"Excluded; confirm applicable tax treatment",
            "roi_summary":{"assumed_monthly_value_inr":float(p.get("assumed_monthly_value_inr",0)),
                "subscription_monthly_inr":price,"assumptions_explicit":True},
            "approval_status":"DRAFT_ONLY","commercial_commitment":False}

    def _onboarding(self,p):
        self._assert_commercial_product(p.get("product_id"),live_action=True)
        product=p["product_id"]
        if product not in {"photo-studio-growth-os","clinics-counter"}: raise ValueError("No registered v1 adapter")
        external=str(p.get("external_tenant_id","")).strip()
        internal=str(p.get("workforce_tenant_id","")).strip()
        if not external or not internal or external==internal: raise ValueError("Distinct verified tenant bindings required")
        payload={"api_version":"v1","product_id":product,"external_tenant_id":external,
            "workforce_tenant_id":internal,"correlation_id":str(p.get("correlation_id") or hashlib.sha256((product+external).encode()).hexdigest()[:24]),
            "provisioning_status":"PENDING_HUMAN_APPROVAL","provision":False}
        payload["payload_sha256"]=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        return {"adapter_payload":payload,"validated":True,"approval_required":True,"provisioned":False}
