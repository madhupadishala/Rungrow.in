from __future__ import annotations

import json
import time

import httpx
import pytest

from app.adapters.generic_product_adapter import AdapterGovernanceError, GenericAdapterContext, GenericProductAdapter
from app.adapters.registry import build_product_adapter_registry
from app.commercial_intelligence import (
    AI_IDENTITY_DISCLOSURE,
    CommercialIntelligenceService,
    MockDirectoryFetcher,
    RobotsCompliantHttpFetcher,
)
from app.model_gateway import ModelGateway, MockModelProvider
from app.policy import Principal
from app.registry import BY_ID
from app.repositories import InMemoryWorkforceRepository


def principal(tenant="tenant-ci", product="photo-studio-growth-os"):
    return Principal("founder", tenant, "founder", frozenset({"*"}), product_ids=frozenset({product}))


@pytest.mark.asyncio
async def test_robots_disallow_blocks_crawl_and_writes_audit_record():
    requested = []

    async def handler(request: httpx.Request):
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private", request=request)
        return httpx.Response(200, text="<title>Forbidden directory listing</title>", headers={"content-type": "text/html"}, request=request)

    transport = httpx.MockTransport(handler)
    repo = InMemoryWorkforceRepository(principal(), {"photo-studio-growth-os"})
    fetcher = RobotsCompliantHttpFetcher(transport=transport, requests_per_second=1000, allow_domains={"directory.example"})
    service = CommercialIntelligenceService(repo, fetcher)
    result = await service.discover(tenant_id="tenant-ci", product_id="photo-studio-growth-os",
        query="wedding photographer", city="Hyderabad",
        source_urls=["https://directory.example/private/studios"])

    assert result["upserted"] == 0
    assert len(result["blocked"]) == 1
    assert requested == ["https://directory.example/robots.txt"]
    assert any(x["action"] == "prospect.discovery.blocked" for x in repo.audit)


@pytest.mark.asyncio
async def test_prospect_deduplication_merges_evidence_and_upgrades_confidence():
    repo = InMemoryWorkforceRepository(principal(), {"photo-studio-growth-os"})
    fetcher = MockDirectoryFetcher([{
        "business_name": "The Wedding Studio",
        "city": "Hyderabad",
        "segment": "wedding photography",
        "source_url": "https://one.example/studio",
        "evidence_urls": ["https://one.example/studio"],
        "evidence_snippet": "Wedding photography and albums",
        "confidence_score": 0.62,
    }])
    service = CommercialIntelligenceService(repo, fetcher)

    first = await service.discover(tenant_id="tenant-ci", product_id="photo-studio-growth-os",
        query="wedding photography", city="Hyderabad")
    fetcher.candidates = [{
        "business_name": "The Wedding Studio",
        "city": "Hyderabad",
        "segment": "wedding photography",
        "source_url": "https://two.example/listing",
        "evidence_urls": ["https://two.example/listing"],
        "evidence_snippet": "Established wedding photography studio with album services",
        "confidence_score": 0.91,
    }]
    second = await service.discover(tenant_id="tenant-ci", product_id="photo-studio-growth-os",
        query="wedding photography", city="Hyderabad")

    rows = await repo.list_prospects("photo-studio-growth-os")
    assert first["upserted"] == second["upserted"] == 1
    assert len(rows) == 1
    assert set(rows[0]["evidence_urls"]) == {
        "https://one.example/studio", "https://two.example/listing"
    }
    assert rows[0]["confidence_score"] == pytest.approx(0.91)
    assert rows[0]["icp_fit_tier"] in {"HIGH", "MEDIUM"}


@pytest.mark.asyncio
async def test_prospects_are_isolated_by_tenant_and_product():
    products = {"photo-studio-growth-os", "clinics-counter"}
    repo_a = InMemoryWorkforceRepository(
        Principal("a", "tenant-a", "founder", frozenset({"*"}), product_ids=frozenset(products)), products)
    repo_b = InMemoryWorkforceRepository(
        Principal("b", "tenant-b", "founder", frozenset({"*"}), product_ids=frozenset(products)), products)
    for product_id, name in (("photo-studio-growth-os", "Studio One"), ("clinics-counter", "Clinic One")):
        await repo_a.upsert_prospect(product_id=product_id, dedupe_key=name.lower(),
            business_name=name, city="Hyderabad", segment="local business",
            decision_maker_name=None, public_contact_channel=None, source_url="https://public.example",
            evidence_urls=["https://public.example"], evidence_snippet="Public listing",
            confidence_score=0.8, icp_fit_tier="HIGH", is_synthetic=False, metadata={})
    assert len(await repo_a.list_prospects("photo-studio-growth-os")) == 1
    assert len(await repo_a.list_prospects("clinics-counter")) == 1
    assert await repo_b.list_prospects() == []
    with pytest.raises(PermissionError):
        await InMemoryWorkforceRepository(
            Principal("limited", "tenant-a", "pgm", frozenset({"lead.enrich"}),
                      product_ids=frozenset({"photo-studio-growth-os"})), products
        ).list_prospects("clinics-counter")


@pytest.mark.asyncio
async def test_growtoys_discovery_only_blocks_live_handoff_but_allows_sandbox():
    products = {"growtoys"}
    p = Principal("founder", "workforce-tenant", "founder", frozenset({"*"}),
                  product_ids=frozenset(products))
    repo = InMemoryWorkforceRepository(p, products)
    fetcher = MockDirectoryFetcher([{
        "business_name": "Learning Toys Hyderabad", "segment": "educational toys",
        "source_url": "https://toys.example", "evidence_urls": ["https://toys.example"],
        "evidence_snippet": "Educational toys retailer",
    }])
    discovery = await CommercialIntelligenceService(repo, fetcher).discover(
        tenant_id=p.tenant_id, product_id="growtoys", query="educational toys",
        city="Hyderabad", is_sandbox=True)
    assert discovery["upserted"] == 1
    assert discovery["outbound_actions"] == 0

    adapter = GenericProductAdapter("growtoys", service_secret="test-secret",
                                    commercial_stage="DISCOVERY_ONLY")
    employee = BY_ID["pgm-growtoys"]
    run = {"id": "sandbox-run", "tenant_id": p.tenant_id, "product_id": "growtoys",
           "employee_id": employee.id, "is_sandbox": True}
    sandbox_context = GenericAdapterContext("external-customer", p.tenant_id, "growtoys",
        "correlation-sandbox", frozenset({"campaign.activate"}), is_sandbox=True)
    result = await adapter.handoff(payload={"prospect_id": "prospect-1"},
        context=sandbox_context, principal=p, employee=employee, run=run,
        approval_id=None, repository=repo)
    assert result["status"] == "SIMULATED"
    assert result["outbound_actions"] == 0

    live_run = {**run, "id": "live-run", "is_sandbox": False}
    live_context = GenericAdapterContext("external-customer", p.tenant_id, "growtoys",
        "correlation-live", frozenset({"campaign.activate"}), is_sandbox=False)
    with pytest.raises(AdapterGovernanceError) as exc:
        await adapter.handoff(payload={"prospect_id": "prospect-1"},
            context=live_context, principal=p, employee=employee, run=live_run,
            approval_id=None, repository=repo)
    assert exc.value.status_code == 403


def test_registry_registers_all_seven_versioned_product_adapters():
    registry = build_product_adapter_registry()
    readiness = registry.readiness()
    assert len(readiness) == 7
    assert {x["product_id"] for x in readiness} == {
        "photo-studio-growth-os", "photo-studio-billing", "clinics-counter",
        "entrepreneur-instagram-tools", "pharma-software-utilities",
        "artists-business-tools", "growtoys",
    }
    assert all(x["api_version"] == "v1" for x in readiness)
    growtoys = next(x for x in readiness if x["product_id"] == "growtoys")
    assert growtoys["commercial_stage"] == "DISCOVERY_ONLY"
    assert growtoys["readiness"] == "DISCOVERY_ONLY"


@pytest.mark.asyncio
async def test_campaign_brief_always_includes_ai_identity_disclosure():
    repo = InMemoryWorkforceRepository(principal(), {"photo-studio-growth-os"})
    provider = MockModelProvider([{
        "name": "Studio owner research",
        "target_icp_segment": "Independent wedding photography studios",
        "channel": "RESEARCH_ONLY",
        "messaging_brief": "Research-only plan to validate workflow needs and buying criteria.",
        "success_metric": "Validated qualified prospects",
    }])
    gateway = ModelGateway({"mock": provider}, primary_provider="mock", repository=repo)
    service = CommercialIntelligenceService(repo, MockDirectoryFetcher(), gateway)
    brief = await service.generate_campaign_brief(tenant_id="tenant-ci",
        product_id="photo-studio-growth-os", segment="wedding photography studios",
        channel="RESEARCH_ONLY", employee_id="pgm-photo-studio-growth-os", is_sandbox=True)
    assert AI_IDENTITY_DISCLOSURE in brief.messaging_brief
    assert "AI" in brief.messaging_brief


@pytest.mark.asyncio
async def test_generic_v1_webhook_is_hmac_signed_and_idempotent():
    p = Principal("service", "workforce-tenant", "service", frozenset({"*"}),
                  actor_type="service", product_ids=frozenset({"clinics-counter"}))
    repo = InMemoryWorkforceRepository(p, {"clinics-counter"})
    adapter = GenericProductAdapter("clinics-counter", service_secret="webhook-secret")
    payload = {
        "tenant_id": "external-clinic-tenant",
        "product_id": "clinics-counter",
        "event": {"event_id": "invoice-1", "event_type": "invoice_recognised",
                  "recognised_revenue_inr": 1250, "metadata": {"invoice_ref": "INV-1"}},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    scopes = frozenset({"clinics-counter:write"})
    signature = adapter.sign(raw, timestamp, external_tenant_id="external-clinic-tenant",
                              workforce_tenant_id="workforce-tenant", scopes=scopes)
    first = await adapter.handle_webhook(raw_body=raw, signature=signature, timestamp=timestamp,
        external_tenant_id="external-clinic-tenant", workforce_tenant_id="workforce-tenant",
        scopes=scopes, idempotency_key="webhook-once", correlation_id="corr-1", repository=repo)
    replay = await adapter.handle_webhook(raw_body=raw, signature=signature, timestamp=timestamp,
        external_tenant_id="external-clinic-tenant", workforce_tenant_id="workforce-tenant",
        scopes=scopes, idempotency_key="webhook-once", correlation_id="corr-1", repository=repo)
    events = await repo.list_product_telemetry()
    assert first["status"] == "ACCEPTED"
    assert replay["replayed"] is True
    assert len(events) == 1
    assert events[0]["recognised_revenue_inr"] == 1250


@pytest.mark.asyncio
async def test_campaign_activation_consumes_one_approval_through_execution_boundary():
    p = principal()
    repo = InMemoryWorkforceRepository(p, {"photo-studio-growth-os"})
    employee = BY_ID["pgm-photo-studio-growth-os"]
    campaign = await repo.create_campaign(product_id="photo-studio-growth-os",
        name="Approved studio campaign", target_icp_segment="Wedding photography studios",
        channel="RESEARCH_ONLY",
        messaging_brief="Research-only brief with AI identity disclosure and no outbound send.",
        ai_identity_disclosure=AI_IDENTITY_DISCLOSURE, is_sandbox=False,
        created_by=p.user_id, metadata={})
    approval = await repo.request_campaign_approval(campaign_id=campaign["id"],
        product_id="photo-studio-growth-os", employee_id=employee.id,
        messaging_brief=campaign["messaging_brief"])
    approval.update(status="APPROVED", approver_id="independent-founder",
                    decided_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    run = await repo.create_agent_run(product_id="photo-studio-growth-os",
        employee_id=employee.id, task="Activate approved campaign", status="planned",
        summary="", steps=[], correlation_id="campaign-activation-run", is_sandbox=False)
    service = CommercialIntelligenceService(repo, MockDirectoryFetcher())
    result = await service.activate_campaign(campaign_id=campaign["id"],
        approval_id=approval["id"], principal=p, employee=employee, run=run)
    assert result["status"] == "ACTIVE"
    assert approval["consumed_at"] is not None
    assert approval["execution_run_id"] == run["id"]
    assert run["execution_state"] == "COMPLETED"
    with pytest.raises(PermissionError):
        await repo.consume_approval(approval["id"], product_id="photo-studio-growth-os",
            employee_id=employee.id, action_type="campaign.activate",
            payload={"campaign_id": campaign["id"], "messaging_brief": campaign["messaging_brief"]},
            run_id=run["id"])
