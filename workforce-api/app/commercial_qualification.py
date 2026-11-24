from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlparse
import httpx
from .commercial_intelligence import DiscoveryBatch, RobotsCompliantHttpFetcher, score_icp


class DiscoveryProvider(Protocol):
    async def discover(self, *, query: str, city: str, product_id: str, source_urls: list[str]) -> DiscoveryBatch: ...


class FixtureDiscoveryProvider:
    """Deterministic test provider. Candidate records are copied, never invented."""
    def __init__(self, candidates=None, blocked=None):
        self.candidates=[dict(x) for x in (candidates or [])]
        self.blocked=list(blocked or [])
        self.calls=[]
    async def discover(self, *, query, city, product_id, source_urls):
        self.calls.append({"query":query,"city":city,"product_id":product_id,"source_urls":list(source_urls)})
        return DiscoveryBatch(candidates=[dict(x) for x in self.candidates],blocked=[dict(x) for x in self.blocked])
    async def verify_source(self, source_url, expected_name=None):
        item=next((x for x in self.candidates if x.get("source_url")==source_url),None)
        if not item: raise ValueError("Fixture has no exact-source verification record")
        name=str(item.get("business_name","")); snippet=str(item.get("raw_evidence_snippet") or item.get("evidence_snippet","")).strip()
        return {"source_url":source_url,"evidence_snippet":snippet,
            "verified_business_identity":bool(name and (not expected_name or name.casefold()==str(expected_name).casefold()) and item.get("verified_business_identity",True)),
            "active_public_presence":bool(item.get("active_public_presence",True) and snippet)}


class SerperDiscoveryProvider:
    """Search via Serper; verify each result page against allowlist, SSRF and robots policy."""
    def __init__(self, *, api_key=None, page_fetcher=None, endpoint="https://google.serper.dev/search", transport=None):
        self.api_key=(api_key or os.getenv("SERPER_API_KEY","")).strip()
        self.page_fetcher=page_fetcher or RobotsCompliantHttpFetcher(
            allow_domains=set(filter(None,os.getenv("RUNGROW_RESEARCH_ALLOW_DOMAINS","").split(","))),
            deny_domains=set(filter(None,os.getenv("RUNGROW_RESEARCH_DENY_DOMAINS","").split(","))))
        self.endpoint,self.transport=endpoint,transport
    async def discover(self, *, query, city, product_id, source_urls):
        if not self.api_key: return DiscoveryBatch(blocked=[{"url":"","reason":"SERPER_API_KEY is not configured"}])
        async with httpx.AsyncClient(timeout=20,follow_redirects=False,transport=self.transport) as client:
            response=await client.post(self.endpoint,headers={"X-API-KEY":self.api_key,"Content-Type":"application/json"},
                json={"q":f"{query} {city}","num":20})
        response.raise_for_status()
        batch=DiscoveryBatch()
        for result in response.json().get("organic",[]):
            url=str(result.get("link","")).strip(); title=str(result.get("title","")).strip()
            snippet=str(result.get("snippet","")).strip()
            if not url or not title or not snippet:
                batch.blocked.append({"url":url,"reason":"Search result missing source URL, title or raw snippet"}); continue
            try: page=await self.page_fetcher.fetch_page(url)
            except Exception as exc:
                batch.blocked.append({"url":url,"reason":str(exc)[:300]}); continue
            page_title=str(page.get("title") or page.get("business_name") or "")
            batch.candidates.append({"business_name":title[:300],"city":city,"segment":query[:160],
                "source_url":url,"evidence_urls":[url],"evidence_snippet":snippet[:2000],
                "raw_evidence_snippet":snippet[:2000],"fetched_at":datetime.now(timezone.utc).isoformat(),
                "verified_business_identity":bool(page_title and title.split(" | ")[0].casefold() in page_title.casefold()),
                "active_public_presence":bool(page_title and page.get("source_url")==url),
                "domain":urlparse(url).hostname,"public_contact_channel":None})
        return batch


class ProspectQualificationService:
    LOCALITIES=("Dilsukhnagar","Chaitanyapuri","LB Nagar","Gaddiannaram","Kothapet","Saroornagar","Nagole","Vanasthalipuram","Hastinapuram")
    def __init__(self, repository, provider): self.repository,self.provider=repository,provider

    def evaluate(self, prospect, evidence):
        text=" ".join(str(prospect.get(k,"")) for k in ("business_name","city","segment","evidence_snippet"))
        locality=next((x for x in self.LOCALITIES if x.casefold() in text.casefold()),None)
        city_match="hyderabad" in str(prospect.get("city","")).casefold() or "hyderabad" in text.casefold()
        fit=score_icp(str(prospect["product_id"]),prospect)["score"]>=0.42
        identity=bool(evidence.get("verified_business_identity") and str(prospect.get("business_name","")).strip())
        active=bool(evidence.get("active_public_presence") and evidence.get("source_url")==prospect.get("source_url") and evidence.get("evidence_snippet"))
        reasons=[]
        if city_match and locality: reasons.append("Hyderabad locality evidenced: "+locality)
        if identity: reasons.append("Business identity corroborated")
        if active: reasons.append("Current public source reverified")
        if fit: reasons.append("Deterministic product-fit signals present")
        qualified=bool(city_match and locality and identity and active and fit and not prospect.get("is_synthetic"))
        return {"status":"QUALIFIED" if qualified else "UNQUALIFIED","score":round(sum((city_match,bool(locality),identity,active,fit))/5,3),
            "reasons":reasons,"locality":locality,"verified_business_identity":identity,
            "active_public_presence":active,"business_fit":fit,"source_url":prospect.get("source_url"),
            "evidence_snippet":evidence.get("evidence_snippet",""),"evaluated_at":datetime.now(timezone.utc).isoformat()}

    async def revalidate(self, *, product_id, limit=300):
        prospects=await self.repository.list_prospects(product_id=product_id,limit=max(1,min(limit,1000)))
        results=[]; qualified=0; unverified=0
        for prospect in prospects:
            try:
                url=str(prospect.get("source_url",""))
                if hasattr(self.provider,"verify_source"):
                    evidence=await self.provider.verify_source(url,expected_name=prospect.get("business_name"))
                elif hasattr(self.provider,"fetch_page") or hasattr(self.provider,"page_fetcher"):
                    fetcher=self.provider if hasattr(self.provider,"fetch_page") else self.provider.page_fetcher
                    page=await fetcher.fetch_page(url); title=str(page.get("title") or page.get("business_name") or "")
                    evidence={"source_url":url,"evidence_snippet":str(page.get("evidence_snippet") or title),
                        "verified_business_identity":bool(title and str(prospect["business_name"]).casefold() in title.casefold()),
                        "active_public_presence":bool(title and page.get("source_url")==url)}
                else: raise ValueError("Provider cannot independently reverify source URL")
                if not evidence.get("verified_business_identity") or not evidence.get("active_public_presence") or not evidence.get("evidence_snippet"):
                    raise ValueError("Historical record has incomplete or unverified public evidence")
                decision=self.evaluate(prospect,evidence)
                await self.repository.update_prospect_qualification(prospect_id=prospect["id"],product_id=product_id,
                    stage=decision["status"],metadata={"revalidation":{"status":"VERIFIED","checked_at":decision["evaluated_at"],
                    "source_url":url,"raw_evidence_snippet":evidence["evidence_snippet"][:2000]},"qualification":decision})
                qualified+=int(decision["status"]=="QUALIFIED")
                results.append({"prospect_id":str(prospect["id"]),**decision})
            except Exception as exc:
                unverified+=1
                await self.repository.update_prospect_qualification(prospect_id=prospect["id"],product_id=product_id,
                    stage="DISCOVERED",metadata={"revalidation":{"status":"UNVERIFIED","reason":str(exc)[:300],
                    "checked_at":datetime.now(timezone.utc).isoformat()}})
                results.append({"prospect_id":str(prospect["id"]),"status":"UNVERIFIED","reason":str(exc)[:300]})
        return {"checked":len(prospects),"qualified":qualified,"unverified":unverified,"results":results}

    async def create_artifacts(self, *, prospect_id, product_id, created_by):
        prospects=await self.repository.list_prospects(product_id=product_id,limit=1000)
        prospect=next((p for p in prospects if str(p.get("id"))==str(prospect_id)),None)
        if not prospect: raise LookupError("Prospect not found in tenant/product scope")
        q=prospect.get("metadata",{}).get("qualification",{})
        if prospect.get("stage")!="QUALIFIED" or prospect.get("is_synthetic") or not q.get("verified_business_identity") or not q.get("active_public_presence"):
            raise PermissionError("Artifacts require a revalidated, non-synthetic QUALIFIED prospect")
        name=str(prospect["business_name"]); source=str(prospect["source_url"])
        content={"prospect_name":name,"qualification_reasons":q.get("reasons",[]),
            "evidence":{"source_url":source,"snippet":prospect.get("evidence_snippet",""),"evidence_urls":prospect.get("evidence_urls",[])},
            "pitch_brief":f"{name} has a verified public photography-business presence. Introduce RunGrow Photo Studio Growth OS as a possible workflow and enquiry follow-up solution; validate actual needs before feature or pricing claims.",
            "whatsapp_draft":f"Hi {name}, I’m an AI assistant acting on behalf of RunGrow. I found your public listing ({source}). We’re researching how photography studios manage enquiries and follow-ups. Would a brief conversation with our team be useful? Human review required.",
            "email_draft":{"subject":f"Studio workflow research — {name}","body":f"Hello {name},\n\nI’m an AI assistant acting on behalf of RunGrow. Your public listing ({source}) indicates a photography business. We’re validating studio workflow and enquiry follow-up needs. A human team member can arrange a discovery conversation if relevant.\n\nRunGrow team"},
            "human_review_required":True,"outbound_dispatch":"BLOCKED_UNTIL_SEPARATE_APPROVAL"}
        specs=(("PITCH_BRIEF","RESEARCH_ONLY","Qualification brief: "+name),("WHATSAPP_DRAFT","WHATSAPP","WhatsApp draft: "+name),("EMAIL_DRAFT","EMAIL","Email draft: "+name))
        artifacts=[]
        for kind,channel,title in specs:
            artifacts.append(await self.repository.create_sales_artifact(prospect_id=prospect_id,product_id=product_id,
                artifact_type=kind,channel=channel,title=title,content=content,created_by=created_by,
                status="PENDING_HUMAN_APPROVAL",metadata={"source_url":source,"qualification_score":q.get("score")}))
        return artifacts
