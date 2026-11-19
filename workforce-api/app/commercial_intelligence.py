"""Compliant public prospect discovery, ICP routing and governed campaign planning."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel, Field

from .model_gateway import AI_DISCLOSURE, ModelGateway

PRODUCT_PLAYBOOKS: dict[str, dict[str, Any]] = {
    "photo-studio-growth-os": {
        "version": "1.0.0", "stage": "COMMERCIAL",
        "segments": ("wedding photography studio", "photo studio", "photographer", "event photography", "photo business"),
        "signals": ("wedding", "photography", "photo studio", "album", "pre-wedding", "photographer"),
    },
    "photo-studio-billing": {
        "version": "1.0.0", "stage": "COMMERCIAL",
        "segments": ("photography billing", "photo lab", "photo studio", "printing studio"),
        "signals": ("photo", "photography", "printing", "album", "studio billing"),
    },
    "clinics-counter": {
        "version": "1.0.0", "stage": "COMMERCIAL",
        "segments": ("clinic", "diagnostic centre", "medical practice", "pharmacy counter"),
        "signals": ("clinic", "doctor", "diagnostic", "medical", "healthcare", "pharmacy"),
    },
    "entrepreneur-instagram-tools": {
        "version": "1.0.0", "stage": "COMMERCIAL",
        "segments": ("small business", "creator", "instagram seller", "local entrepreneur"),
        "signals": ("instagram", "creator", "boutique", "salon", "small business", "online store"),
    },
    "pharma-software-utilities": {
        "version": "1.0.0", "stage": "COMMERCIAL",
        "segments": ("pharmaceutical company", "pharma distributor", "pharmacovigilance", "medical representative"),
        "signals": ("pharma", "pharmaceutical", "drug safety", "pharmacovigilance", "medical representative"),
    },
    "artists-business-tools": {
        "version": "1.0.0", "stage": "COMMERCIAL",
        "segments": ("artist", "performer", "creative professional", "music academy"),
        "signals": ("artist", "performer", "musician", "dance", "music academy", "creative"),
    },
    "growtoys": {
        "version": "1.0.0", "stage": "DISCOVERY_ONLY",
        "segments": ("toy retailer", "toy manufacturer", "children's products", "educational toys"),
        "signals": ("toy", "toys", "children", "kids", "play", "educational"),
    },
}
AI_IDENTITY_DISCLOSURE = (
    "I am an AI assistant acting on behalf of RunGrow. I can help explain the product and "
    "arrange a human follow-up; I am not a human representative."
)


class RobotsDisallowed(PermissionError):
    def __init__(self, url: str, reason: str = "robots.txt disallows this URL"):
        super().__init__(reason)
        self.url = url
        self.reason = reason


@dataclass
class DiscoveryBatch:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, str]] = field(default_factory=list)


class DirectoryFetcher(Protocol):
    async def discover(self, *, query: str, city: str, product_id: str,
                       source_urls: list[str]) -> DiscoveryBatch: ...


class _PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = False
        self.title_text = ""
        self.meta: dict[str, str] = {}
        self.anchors: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "title":
            self.title = True
        if tag == "meta" and values.get("name", "").lower() in {"description", "telephone"}:
            self.meta[values["name"].lower()] = values.get("content", "")
        if tag == "meta" and values.get("property", "").lower() in {"og:title", "og:description"}:
            self.meta[values["property"].lower()] = values.get("content", "")
        if tag == "a" and values.get("href"):
            self.anchors.append(values["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self.title = False

    def handle_data(self, data):
        if self.title:
            self.title_text += data


class RobotsCompliantHttpFetcher:
    """Fetches only public HTTP(S) pages allowed by robots.txt and domain policy.

    This adapter deliberately does not provide search-engine scraping. Supply
    directory URLs from an approved search provider or public directory index.
    """
    def __init__(self, *, user_agent: str = "RunGrowResearchBot/1.0 (+https://rungrow.in)",
                 requests_per_second: float = 0.5, allow_domains: set[str] | None = None,
                 deny_domains: set[str] | None = None, timeout_seconds: float = 12.0):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.user_agent = user_agent
        self.interval = 1.0 / requests_per_second
        self.allow_domains = {x.lower().lstrip(".") for x in (allow_domains or set())}
        self.deny_domains = {x.lower().lstrip(".") for x in (deny_domains or set())}
        self.timeout_seconds = timeout_seconds
        self._last_request: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, RobotFileParser] = {}
        self.audit_events: list[dict[str, Any]] = []

    @staticmethod
    def _host_allowed(host: str, domains: set[str]) -> bool:
        return any(host == domain or host.endswith("." + domain) for domain in domains)

    def _validate_url(self, url: str) -> tuple[str, str]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only credential-free public HTTP(S) URLs are allowed")
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost"} or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("Private or local network targets are prohibited")
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                raise ValueError("Private or non-global IP targets are prohibited")
        except ValueError as exc:
            if "does not appear to be an IPv4 or IPv6 address" not in str(exc):
                raise
        if self.allow_domains and not self._host_allowed(host, self.allow_domains):
            raise PermissionError("Domain is not on the configured allow list")
        if self._host_allowed(host, self.deny_domains):
            raise PermissionError("Domain is on the configured deny list")
        normalized = urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))
        return normalized, host

    async def _throttle(self, host: str):
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self.interval - (time.monotonic() - self._last_request.get(host, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()

    async def _get_robots(self, host: str, scheme: str) -> RobotFileParser:
        origin = f"{scheme}://{host}"
        if origin in self._robots:
            return self._robots[origin]
        parser = RobotFileParser()
        robots_url = origin + "/robots.txt"
        await self._throttle(host)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False,
                                         headers={"User-Agent": self.user_agent}) as client:
                response = await client.get(robots_url)
            if response.status_code == 404:
                parser.parse([])
            elif response.status_code in {401, 403, 429} or response.status_code >= 500:
                parser.parse(["User-agent: *", "Disallow: /"])
            elif response.is_redirect:
                parser.parse(["User-agent: *", "Disallow: /"])
            else:
                response.raise_for_status()
                parser.parse(response.text.splitlines())
        except (httpx.HTTPError, OSError):
            parser.parse(["User-agent: *", "Disallow: /"])
        parser.set_url(robots_url)
        self._robots[origin] = parser
        return parser

    async def fetch_page(self, url: str) -> dict[str, Any]:
        normalized, host = self._validate_url(url)
        scheme = urlparse(normalized).scheme
        robots = await self._get_robots(host, scheme)
        if not robots.can_fetch(self.user_agent, normalized):
            raise RobotsDisallowed(normalized)
        await self._throttle(host)
        async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False,
                                     headers={"User-Agent": self.user_agent}) as client:
            response = await client.get(normalized)
        if response.is_redirect:
            raise PermissionError("Redirects are not followed; revalidate destination through the allow/deny policy")
        response.raise_for_status()
        if "text/html" not in response.headers.get("content-type", "").lower():
            raise ValueError("Only public HTML directory pages are supported")
        parser = _PageMetadata()
        parser.feed(response.text[:1_000_000])
        name = (parser.meta.get("og:title") or parser.title_text or "").strip()[:300]
        snippet = (parser.meta.get("description") or parser.meta.get("og:description") or "").strip()[:2000]
        if not name:
            raise ValueError("Directory page did not expose a usable business title")
        return {"business_name": name, "evidence_snippet": snippet, "source_url": normalized,
                "domain": host, "city": "", "public_contact_channel": None,
                "decision_maker_name": None, "evidence_urls": [normalized]}

    async def discover(self, *, query: str, city: str, product_id: str,
                       source_urls: list[str]) -> DiscoveryBatch:
        batch = DiscoveryBatch()
        for url in source_urls:
            try:
                candidate = await self.fetch_page(url)
                candidate["city"] = city
                candidate["segment"] = query[:160]
                batch.candidates.append(candidate)
            except RobotsDisallowed as exc:
                batch.blocked.append({"url": exc.url, "reason": exc.reason})
            except (ValueError, PermissionError, httpx.HTTPError) as exc:
                batch.blocked.append({"url": url, "reason": str(exc)[:300]})
        return batch


class MockDirectoryFetcher:
    """Deterministic offline fixture fetcher; never opens a network connection."""
    def __init__(self, candidates: list[dict[str, Any]] | None = None,
                 blocked: list[dict[str, str]] | None = None):
        self.candidates = list(candidates or [])
        self.blocked = list(blocked or [])
        self.calls: list[dict[str, Any]] = []

    async def discover(self, *, query: str, city: str, product_id: str,
                       source_urls: list[str]) -> DiscoveryBatch:
        self.calls.append({"query": query, "city": city, "product_id": product_id,
                           "source_urls": list(source_urls)})
        return DiscoveryBatch(candidates=[dict(x) for x in self.candidates],
                              blocked=[dict(x) for x in self.blocked])


def _norm(value: str | None) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _normalized_phone(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def prospect_dedupe_key(candidate: dict[str, Any], city: str) -> str:
    # Business-name + city is stable across multiple public source domains.
    # Domain and phone aliases are retained in metadata for later reconciliation.
    identity = _norm(str(candidate.get("business_name", "")))
    city_key = _norm(city)
    if not identity or not city_key:
        raise ValueError("Business name and city are required for deterministic deduplication")
    return hashlib.sha256(f"{identity}|{city_key}".encode("utf-8")).hexdigest()


def score_icp(product_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
    playbook = PRODUCT_PLAYBOOKS.get(product_id)
    if not playbook:
        raise ValueError("Unknown product playbook")
    text = " ".join(str(candidate.get(k, "")) for k in ("business_name", "segment", "evidence_snippet", "description")).lower()
    hits = sum(1 for signal in playbook["signals"] if signal in text)
    score = min(1.0, 0.18 + hits * 0.16)
    tier = "HIGH" if score >= 0.66 else "MEDIUM" if score >= 0.42 else "LOW"
    return {"score": round(score, 3), "tier": tier}


def route_product_fit(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    scores = []
    for product_id in PRODUCT_PLAYBOOKS:
        result = score_icp(product_id, candidate)
        scores.append({"product_id": product_id, **result,
                       "commercial_stage": PRODUCT_PLAYBOOKS[product_id]["stage"],
                       "playbook_version": PRODUCT_PLAYBOOKS[product_id]["version"]})
    return sorted(scores, key=lambda x: (x["score"], x["product_id"]), reverse=True)


class CampaignBrief(BaseModel):
    name: str = Field(min_length=3, max_length=240)
    target_icp_segment: str = Field(min_length=2, max_length=240)
    channel: str = Field(pattern="^(EMAIL|WHATSAPP|PHONE|SOCIAL|IN_PRODUCT|PARTNER|RESEARCH_ONLY)$")
    messaging_brief: str = Field(min_length=20, max_length=10000)
    success_metric: str = Field(min_length=2, max_length=240)


class CommercialIntelligenceService:
    def __init__(self, repository, fetcher: DirectoryFetcher, model_gateway: ModelGateway | None = None):
        self.repository = repository
        self.fetcher = fetcher
        self.model_gateway = model_gateway

    async def discover(self, *, tenant_id: str, product_id: str, query: str, city: str,
                       source_urls: list[str] | None = None, is_sandbox: bool = False) -> dict[str, Any]:
        if product_id not in PRODUCT_PLAYBOOKS:
            raise ValueError("Unknown product scope")
        if not query.strip() or not city.strip():
            raise ValueError("Discovery query and city are required")
        # Discovery is read-only regardless of mode. Sandbox records are marked synthetic.
        batch = await self.fetcher.discover(query=query, city=city, product_id=product_id,
                                            source_urls=source_urls or [])
        for blocked in batch.blocked:
            await self.repository.append_audit("prospect.discovery.blocked", "public_source",
                blocked.get("url", "unknown"), product_id,
                {"reason": blocked.get("reason", "blocked"), "robots_policy": "fail_closed"})
        persisted = []
        for candidate in batch.candidates:
            name = str(candidate.get("business_name", "")).strip()
            source_url = str(candidate.get("source_url", "")).strip()
            if not name or not source_url:
                continue
            fit = score_icp(product_id, {**candidate, "segment": candidate.get("segment") or query})
            key = prospect_dedupe_key(candidate, city)
            evidence_urls = candidate.get("evidence_urls") or [source_url]
            record = await self.repository.upsert_prospect(
                product_id=product_id, dedupe_key=key, business_name=name, city=city,
                segment=str(candidate.get("segment") or query)[:160],
                decision_maker_name=candidate.get("decision_maker_name"),
                public_contact_channel=candidate.get("public_contact_channel"),
                source_url=source_url, evidence_urls=evidence_urls,
                evidence_snippet=str(candidate.get("evidence_snippet", ""))[:2000],
                confidence_score=float(candidate.get("confidence_score", fit["score"])),
                icp_fit_tier=fit["tier"], is_synthetic=bool(is_sandbox or candidate.get("is_synthetic", False)),
                metadata={"normalized_domain": _norm(candidate.get("domain") or urlparse(source_url).hostname),
                          "normalized_phone": _normalized_phone(candidate.get("phone")),
                          "product_fit": route_product_fit(candidate),
                          "playbook_version": PRODUCT_PLAYBOOKS[product_id]["version"]})
            persisted.append(record)
        await self.repository.append_audit("prospect.discovery.completed", "prospect_batch",
            hashlib.sha256(f"{tenant_id}|{product_id}|{query}|{city}".encode()).hexdigest()[:32],
            product_id, {"found": len(batch.candidates), "persisted": len(persisted),
                         "blocked": len(batch.blocked), "is_sandbox": is_sandbox})
        return {"product_id": product_id, "discovered": len(batch.candidates),
                "upserted": len(persisted), "blocked": batch.blocked,
                "prospects": persisted, "outbound_actions": 0, "is_sandbox": is_sandbox}

    async def generate_campaign_brief(self, *, tenant_id: str, product_id: str, segment: str,
                                      channel: str, employee_id: str, is_sandbox: bool = True) -> CampaignBrief:
        if not self.model_gateway:
            raise RuntimeError("ModelGateway is required for campaign generation")
        if product_id not in PRODUCT_PLAYBOOKS:
            raise ValueError("Unknown product scope")
        if product_id == "growtoys" and PRODUCT_PLAYBOOKS[product_id]["stage"] == "DISCOVERY_ONLY" and not is_sandbox:
            raise PermissionError("Growtoys is DISCOVERY_ONLY; live campaigns are blocked")
        brief, _ = await self.model_gateway.generate_structured(
            messages=[{"role": "user", "content": (
                f"Create a campaign planning brief only; do not send messages. Product={product_id}; "
                f"ICP segment={segment}; channel={channel}. Include a measurable success metric. "
                f"Mandatory outbound disclosure: {AI_IDENTITY_DISCLOSURE}"
            )}],
            output_model=CampaignBrief, tenant_id=tenant_id, product_id=product_id,
            employee_id=employee_id, run_id=f"campaign-brief:{hashlib.sha256((tenant_id+product_id+segment).encode()).hexdigest()[:24]}",
            outbound=True, is_sandbox=is_sandbox)
        brief.messaging_brief = brief.messaging_brief.strip() + "\n\nAI identity disclosure: " + AI_IDENTITY_DISCLOSURE
        return brief

    async def create_campaign(self, *, product_id: str, brief: CampaignBrief, is_sandbox: bool,
                              created_by: str) -> dict[str, Any]:
        if product_id == "growtoys" and PRODUCT_PLAYBOOKS[product_id]["stage"] == "DISCOVERY_ONLY" and not is_sandbox:
            raise PermissionError("Growtoys is DISCOVERY_ONLY; live campaign activation is blocked")
        return await self.repository.create_campaign(
            product_id=product_id, name=brief.name, target_icp_segment=brief.target_icp_segment,
            channel=brief.channel, messaging_brief=brief.messaging_brief,
            ai_identity_disclosure=AI_IDENTITY_DISCLOSURE, is_sandbox=is_sandbox,
            created_by=created_by, metadata={"success_metric": brief.success_metric,
                "playbook_version": PRODUCT_PLAYBOOKS[product_id]["version"]})

    async def request_campaign_approval(self, *, campaign: dict[str, Any], employee_id: str) -> dict[str, Any]:
        if campaign.get("is_sandbox"):
            raise PermissionError("Sandbox campaigns cannot request live activation approval")
        if campaign.get("product_id") == "growtoys" and PRODUCT_PLAYBOOKS["growtoys"]["stage"] == "DISCOVERY_ONLY":
            raise PermissionError("Growtoys is DISCOVERY_ONLY; live campaign approval is blocked")
        return await self.repository.request_campaign_approval(campaign_id=str(campaign["id"]),
            product_id=campaign["product_id"], employee_id=employee_id,
            messaging_brief=campaign["messaging_brief"])

    async def activate_campaign(self, *, campaign_id: str, approval_id: str, principal,
                                employee, run: dict[str, Any]) -> dict[str, Any]:
        from .execution_boundary import ExecutionBoundaryGuard, ToolDefinition, ToolRegistry
        campaign = await self.repository.get_campaign(campaign_id)
        if not campaign:
            raise LookupError("Campaign not found")
        if campaign["product_id"] == "growtoys" and PRODUCT_PLAYBOOKS["growtoys"]["stage"] == "DISCOVERY_ONLY":
            raise PermissionError("Growtoys is DISCOVERY_ONLY; live campaign activation is blocked")
        registry = ToolRegistry()
        async def activate(campaign_id: str, approval_id: str):
            approved = await self.repository.transition_campaign(
                campaign_id, status="APPROVED", approval_id=approval_id,
                execution_run_id=str(run["id"]))
            return await self.repository.transition_campaign(
                campaign_id, status="ACTIVE", approval_id=approval_id,
                execution_run_id=str(run["id"]))
        registry.register(ToolDefinition("campaign.activate", frozenset({"campaign.activate"}),
            True, False, "campaign.activate", activate, frozenset({"Sales & Revenue","Growth & Marketing"})))
        return await ExecutionBoundaryGuard(self.repository, registry).execute(
            tool_name="campaign.activate", principal=principal, employee=employee, run=run,
            payload={"campaign_id": campaign_id, "approval_id": approval_id},
            approval_id=approval_id)
