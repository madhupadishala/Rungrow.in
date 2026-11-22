import json
import httpx
import pytest

from app.commercial_intelligence import TinyFishResearchProvider


@pytest.mark.asyncio
async def test_tinyfish_provider_extracts_allowlisted_public_business_and_source_provenance():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "status": "COMPLETED",
            "numOfSteps": 2,
            "result": {"businesses": [{
                "business_name": "Public Studio",
                "city": "Hyderabad",
                "category": "Photography studio",
                "public_phone": "+91 40 1234 5678",
                "public_website": "https://studio.example.org"
            }]}
        })

    provider = TinyFishResearchProvider(
        api_key="fixture-key",
        allow_domains={"directory.example.org"},
        max_steps=10,
        transport=httpx.MockTransport(handler),
    )
    batch = await provider.discover(
        query="photography studio",
        city="Hyderabad",
        product_id="photo-studio-growth-os",
        source_urls=["https://directory.example.org/list#top"],
    )

    assert len(requests) == 1
    assert requests[0].headers["x-api-key"] == "fixture-key"
    assert batch.candidates[0]["business_name"] == "Public Studio"
    assert batch.candidates[0]["city"] == "Hyderabad"
    assert batch.candidates[0]["category"] == "Photography studio"
    assert batch.candidates[0]["phone"] == "+91 40 1234 5678"
    assert batch.candidates[0]["website"] == "https://studio.example.org"
    assert batch.candidates[0]["source_url"] == "https://directory.example.org/list"
    assert batch.candidates[0]["evidence_urls"] == [batch.candidates[0]["source_url"]]
    assert batch.usage == [{"steps": 2}]


@pytest.mark.asyncio
async def test_tinyfish_provider_fails_closed_without_allowlist_or_key():
    provider = TinyFishResearchProvider(api_key="", allow_domains=set())
    batch = await provider.discover(
        query="clinic", city="Hyderabad", product_id="clinics-counter",
        source_urls=["https://directory.example.org/clinics"],
    )
    assert not batch.candidates
    assert batch.blocked
    assert "TINYFISH_API_KEY" in batch.blocked[0]["reason"] or "allowlist" in batch.blocked[0]["reason"].lower()


@pytest.mark.asyncio
async def test_tinyfish_provider_blocks_non_allowlisted_domains_before_network():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status":"COMPLETED","result":{"businesses":[]}})
    provider = TinyFishResearchProvider(
        api_key="fixture-key", allow_domains={"allowed.example.org"},
        transport=httpx.MockTransport(handler),
    )
    batch = await provider.discover(
        query="clinic", city="Hyderabad", product_id="clinics-counter",
        source_urls=["https://unapproved.example.net/list"],
    )
    assert not calls
    assert not batch.candidates
    assert "not allowlisted" in batch.blocked[0]["reason"]
