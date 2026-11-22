"""Workforce 2.2 runtime skill, commercial governance and PostgreSQL audit tests."""
import os
from uuid import uuid4
import pytest
import httpx
from pydantic import BaseModel
from app.model_gateway import ModelGateway, OpenAICompatibleProvider, MockModelProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.policy import Principal
from app.repositories import PostgresWorkforceRepository
from app.registry import BY_ID
from app.skill_registry import resolve_runtime_skill_bundle
from app.tool_registry import WorkforceToolRegistry, ToolAuthorizationError

DB_URL=os.getenv("RUNGROW_TEST_DATABASE_URL")

def test_runtime_skill_bundle_is_versioned_and_product_scoped():
    commercial=resolve_runtime_skill_bundle("Sales & Revenue","photo-studio-growth-os","outbound-sdr")
    discovery=resolve_runtime_skill_bundle("Sales & Revenue","growtoys","outbound-sdr")
    assert len(commercial["skill_version_hash"])==64
    assert commercial["skill_version_hash"] != discovery["skill_version_hash"]
    assert any("DISCOVERY_ONLY" in x for x in discovery["instructions"])
    assert any("single-use approval" in x for x in commercial["instructions"])

def test_commercial_tool_catalogue_and_ai_disclosure(monkeypatch):
    tools=WorkforceToolRegistry()
    assert {"account_research_brief","personalized_outreach_draft","commercial_proposal_builder","product_onboarding_trigger"}.issubset(tools.names())
    principal=frozenset({"*"})
    research,_,_=tools.execute(name="account_research_brief",employee=BY_ID["lead-research"],
        principal_permissions=principal,tenant_id="tenant-a",payload={"product_id":"photo-studio-growth-os","city":"Hyderabad",
        "candidates":[{"business_name":"Studio A","city":"Hyderabad","segment":"wedding photography",
        "source_url":"https://directory.example.test/studio","evidence_snippet":"Public listing"}]})
    assert research["evidence_status"]=="VERIFIED"
    outreach,_,_=tools.execute(name="personalized_outreach_draft",employee=BY_ID["outbound-sdr"],
        principal_permissions=principal,tenant_id="tenant-a",payload={"product_id":"photo-studio-growth-os","channel":"whatsapp","prospect":{"city":"Hyderabad"}})
    assert outreach["approval_required"] is True and outreach["sent"] is False
    assert "AI" in outreach["draft"] or "artificial intelligence" in outreach["draft"].lower()
    proposal,_,_=tools.execute(name="commercial_proposal_builder",employee=BY_ID["ae"],
        principal_permissions=principal,tenant_id="tenant-a",payload={"product_id":"photo-studio-growth-os","tier":"Starter","months":2})
    assert proposal["currency"] == "INR" and proposal["subtotal_inr"] == 9998
    assert proposal["line_items"][0]["unit_price_inr"] == 4999
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET", "test-adapter-secret")
    handoff,_,_=tools.execute(name="product_onboarding_trigger",employee=BY_ID["onboarding"],
        principal_permissions=principal,tenant_id="tenant-a",payload={"product_id":"photo-studio-growth-os",
        "external_tenant_id":"customer-tenant","workforce_tenant_id":"workforce-tenant"})
    assert handoff["validated"] and handoff["provisioned"] is False
    assert handoff["signature_headers"]["X-RunGrow-Signature"].startswith("sha256=")
    assert handoff["signature_headers"]["X-RunGrow-Timestamp"]
    assert handoff["signature_headers"]["Idempotency-Key"]


@pytest.mark.parametrize("tool,employee,payload",[
    ("personalized_outreach_draft","outbound-sdr",{"product_id":"growtoys","channel":"email"}),
    ("commercial_proposal_builder","ae",{"product_id":"growtoys","tier":"Starter"}),
    ("product_onboarding_trigger","onboarding",{"product_id":"growtoys","external_tenant_id":"x","workforce_tenant_id":"y"}),
])
def test_growtoys_discovery_only_hard_blocks_commercial_actions(tool,employee,payload):
    with pytest.raises(ToolAuthorizationError):
        WorkforceToolRegistry().execute(name=tool,employee=BY_ID[employee],
            principal_permissions=frozenset({"*"}),tenant_id="tenant-a",payload=payload)

@pytest.mark.asyncio
async def test_postgres16_skill_audit_table_is_tenant_scoped():
    assert DB_URL, "CI must supply PostgreSQL 16 RUNGROW_TEST_DATABASE_URL"
    engine=create_async_engine(DB_URL,pool_pre_ping=True)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    tenant="w22-audit-"+uuid4().hex
    principal=Principal("w22-test",tenant,"founder",frozenset({"*"}))
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session,principal):
                result=await session.execute(text("SELECT count(*) FROM employee_tool_executions"))
                assert result.scalar_one()==0
                bundle=resolve_runtime_skill_bundle("Growth & Marketing","photo-studio-growth-os","market-intel")
                assert len(bundle["skill_version_hash"])==64
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_non_mock_model_gateway_structured_ceo_department_and_supervisor_paths():
    class CEOPlan(BaseModel):
        objectives: list[str]
    class DepartmentAssignment(BaseModel):
        specialists: list[str]
    class SupervisorReview(BaseModel):
        accepted: bool
        supervisor_feedback: str

    responses = [
        '{"objectives":["Research ICP","Prepare evidence"]}',
        '{"specialists":["market-intel","lead-research"]}',
        'not-json',
        '{"accepted":false,"supervisor_feedback":"Add attributable source URLs and a confidence score."}',
    ]
    calls = []
    def handler(request):
        calls.append(request)
        content = responses.pop(0)
        return httpx.Response(200, json={"choices":[{"message":{"content":content}}],"usage":{"prompt_tokens":10,"completion_tokens":5}})
    transport = httpx.MockTransport(handler)
    provider = OpenAICompatibleProvider(base_url="https://model.test/v1", api_key="fixture", transport=transport)
    gateway = ModelGateway({"openai-compatible":provider}, primary_provider="openai-compatible",
        primary_model="fixture-model")
    common = {"tenant_id":"tenant-w22","product_id":"photo-studio-growth-os","employee_id":"digital-ceo","run_id":"run-w22"}
    ceo,_ = await gateway.generate_structured(messages=[{"role":"user","content":"decompose objective"}],
        output_model=CEOPlan, **common)
    assignment,_ = await gateway.generate_structured(messages=[{"role":"user","content":"assign specialists"}],
        output_model=DepartmentAssignment, **common)
    review,_ = await gateway.generate_structured(messages=[{"role":"user","content":"review evidence"}],
        output_model=SupervisorReview, **common)
    assert ceo.objectives == ["Research ICP","Prepare evidence"]
    assert assignment.specialists == ["market-intel","lead-research"]
    assert review.accepted is False and "source URLs" in review.supervisor_feedback
    assert len(calls) == 4  # three live HTTP calls plus strict-schema JSON repair

@pytest.mark.asyncio
async def test_non_mock_model_gateway_uses_deterministic_fallback_on_transient_http_failure():
    def unavailable(request):
        return httpx.Response(503, json={"error":"temporarily unavailable"})
    primary = OpenAICompatibleProvider(base_url="https://model.test/v1",
        transport=httpx.MockTransport(unavailable))
    fallback = MockModelProvider([{"value":"fallback"}])
    class Result(BaseModel):
        value: str
    gateway = ModelGateway({"openai-compatible":primary,"mock":fallback},
        primary_provider="openai-compatible",primary_model="fixture",
        fallback_provider="mock",fallback_model="fallback")
    result,_ = await gateway.generate_structured(messages=[{"role":"user","content":"test fallback"}],
        output_model=Result,tenant_id="tenant-w22",product_id=None,
        employee_id="digital-ceo",run_id="fallback-run")
    assert result.value == "fallback"
