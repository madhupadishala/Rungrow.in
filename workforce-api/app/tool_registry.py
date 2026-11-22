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
