"""Workforce 2.2 runtime skill, commercial governance and PostgreSQL audit tests."""
import os
from uuid import uuid4
import pytest
import httpx
from pydantic import BaseModel
from app.model_gateway import ModelGateway, OpenAICompatibleProvider, MockModelProvider
from app.adapters.base import SignedWebhookVerifier
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.policy import Principal
from app.repositories import PostgresWorkforceRepository
from app.registry import BY_ID
from app.skill_registry import resolve_runtime_skill_bundle
from app.tool_registry import WorkforceToolRegistry, ToolAuthorizationError
from app.orchestration import WorkforceOrchestrator
from app.main import approve_single_use_outbox_action, SingleUseApprovalRequest

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
    signed_body = __import__("json").dumps(handoff["adapter_payload"],sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
    SignedWebhookVerifier("test-adapter-secret").verify(signed_body,
        handoff["signature_headers"]["X-RunGrow-Signature"],
        handoff["signature_headers"]["X-RunGrow-Timestamp"],
        tenant_id="customer-tenant",product_id="photo-studio-growth-os",
        workforce_tenant_id="workforce-tenant",scopes=frozenset({"tenant.provision"}))


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


@pytest.mark.asyncio
async def test_postgres16_department_head_executes_all_assigned_specialists_and_persists_combined_evidence():
    assert DB_URL, "CI must supply PostgreSQL 16 RUNGROW_TEST_DATABASE_URL"
    engine=create_async_engine(DB_URL,pool_pre_ping=True)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    tenant="w22-fanout-"+uuid4().hex
    directive_id=str(uuid4())
    order_id=str(uuid4())
    principal=Principal("w22-founder",tenant,"founder",frozenset({"*"}),product_ids=frozenset({"photo-studio-growth-os"}))
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session,principal) as repo:
                await session.execute(text("""INSERT INTO executive_directives
                  (id,tenant_id,product_id,founder_id,command,status)
                  VALUES (:id,:tenant,'photo-studio-growth-os','w22-founder',
                    'Qualify prospects and prepare an outreach draft','DELEGATED')"""),
                  {"id":directive_id,"tenant":tenant})
                payload={"product_id":"photo-studio-growth-os","candidates":[
                    {"business_name":"Studio Alpha","city":"Hyderabad","source_url":"https://example.test/alpha"}],
                    "channel":"email","body":"Hello, we can help improve studio operations.",
                    "delegated_specialists":[
                      {"employee_id":"lead-research","tool_name":"icp_lead_scoring","task":"Score evidence-backed ICP fit"},
                      {"employee_id":"outbound-sdr","tool_name":"personalized_outreach_draft","task":"Prepare disclosed outreach"}]}
                row=await session.execute(text("""INSERT INTO department_work_orders
                  (id,tenant_id,directive_id,product_id,department,department_head_id,title,objective,
                   dependencies,specialist_role_id,tool_name,tool_input,max_budget_cents,spent_cents,
                   status,claimed_by,locked_by_employee_id,revision_count)
                  VALUES (:id,:tenant,:directive,'photo-studio-growth-os','Sales & Revenue','cro',
                   'Sales evidence','Qualify prospects','[]'::jsonb,'lead-research','icp_lead_scoring',
                   CAST(:payload AS jsonb),1000,0,'IN_PROGRESS','w22-worker','w22-worker',0)
                  RETURNING *"""),
                  {"id":order_id,"tenant":tenant,"directive":directive_id,"payload":__import__("json").dumps(payload)})
                order=dict(row.mappings().one())
                service=WorkforceOrchestrator(repo,principal)
                original_acceptance=service._acceptance
                first_review={"outbound-sdr":True}
                def force_one_revision(tool_name,tool_result):
                    if tool_name=="personalized_outreach_draft" and first_review["outbound-sdr"]:
                        first_review["outbound-sdr"]=False
                        return False
                    return original_acceptance(tool_name,tool_result)
                service._acceptance=force_one_revision
                result=await service.execute_work_order(order)
                assert result["status"]=="APPROVED_BY_HEAD"
                assert result["deliverable"]["evidence_type"]=="departmental_combined_evidence"
                assert result["deliverable"]["specialist_count"]==2
                proofs=(await session.execute(text("""SELECT employee_id,input_evidence,output_evidence,status
                  FROM employee_tool_executions WHERE tenant_id=:tenant AND work_order_id=:order
                  ORDER BY created_at"""),{"tenant":tenant,"order":order_id})).mappings().all()
                assert {x["employee_id"] for x in proofs}=={"lead-research","outbound-sdr"}
                assert all(len(x["input_evidence"]["skill_version_hash"])==64 for x in proofs)
                outbound_proofs=[x for x in proofs if x["employee_id"]=="outbound-sdr"]
                assert len(outbound_proofs)==2
                assert outbound_proofs[0]["status"]=="FAILED"
                assert outbound_proofs[1]["status"]=="PASSED"
                assert "supervisor_feedback" in outbound_proofs[1]["input_evidence"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres16_outreach_approval_dispatch_is_single_use_and_fails_closed(monkeypatch):
    assert DB_URL, "CI must supply PostgreSQL 16 RUNGROW_TEST_DATABASE_URL"
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN",raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID",raising=False)
    engine=create_async_engine(DB_URL,pool_pre_ping=True)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    tenant="w22-outreach-"+uuid4().hex
    directive_id=str(uuid4()); order_id=str(uuid4())
    founder=Principal("w22-founder",tenant,"founder",frozenset({"*"}),
        product_ids=frozenset({"photo-studio-growth-os"}))
    agent=Principal("outbound-sdr",tenant,"agent",frozenset({"*"}),
        actor_type="agent",product_ids=frozenset({"photo-studio-growth-os"}))
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session,agent) as repo:
                await session.execute(text("""INSERT INTO executive_directives
                  (id,tenant_id,product_id,founder_id,command,status)
                  VALUES (:id,:tenant,'photo-studio-growth-os','w22-founder','Prepare outreach','DELEGATED')"""),
                  {"id":directive_id,"tenant":tenant})
                await session.execute(text("""INSERT INTO department_work_orders
                  (id,tenant_id,directive_id,product_id,department,department_head_id,title,objective,
                   dependencies,specialist_role_id,tool_name,tool_input,max_budget_cents,spent_cents,status,revision_count)
                  VALUES (:id,:tenant,:directive,'photo-studio-growth-os','Sales & Revenue','cro',
                   'Outreach','Prepare approved outreach','[]'::jsonb,'outbound-sdr','personalized_outreach_draft',
                   '{}'::jsonb,100,0,'IN_PROGRESS',0)"""),
                  {"id":order_id,"tenant":tenant,"directive":directive_id})
                payload={"to":"+15551234567","message":"Hello. I am RunGrow's AI assistant."}
                approval=await repo.create_approval(product_id="photo-studio-growth-os",
                    employee_id="outbound-sdr",action_type="whatsapp.send",payload=payload,
                    expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)+__import__("datetime").timedelta(hours=1))
                await session.execute(text("""INSERT INTO wf_outbox_events
                  (id,tenant_id,product_id,directive_id,work_order_id,approval_id,employee_id,
                   action_type,payload_hash,payload,status)
                  VALUES (:id,:tenant,'photo-studio-growth-os',:directive,:order,:approval,
                   'outbound-sdr','whatsapp.send',:hash,CAST(:payload AS jsonb),'QUEUED_FOR_APPROVAL')"""),
                  {"id":str(uuid4()),"tenant":tenant,"directive":directive_id,"order":order_id,
                   "approval":approval["id"],"hash":approval["payload_hash"],
                   "payload":__import__("json").dumps({"channel":"whatsapp","dispatch_payload":payload})})
                repo.principal=founder
                with pytest.raises(Exception) as raised:
                    await approve_single_use_outbox_action(approval["id"],SingleUseApprovalRequest(),founder,repo)
                assert getattr(raised.value,"status_code",None)==503
                outbox=(await session.execute(text("SELECT status FROM wf_outbox_events WHERE approval_id=:id"),
                    {"id":approval["id"]})).scalar_one()
                assert outbox=="BLOCKED_UNCONFIGURED"
                approved=(await session.execute(text("SELECT status FROM wf_approvals WHERE id=:id"),
                    {"id":approval["id"]})).scalar_one()
                assert approved=="APPROVED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres16_signed_onboarding_approval_consumes_once_and_records_blocked_dispatch(monkeypatch):
    assert DB_URL, "CI must supply PostgreSQL 16 RUNGROW_TEST_DATABASE_URL"
    monkeypatch.delenv("RUNGROW_PRODUCT_ADAPTER_DISPATCH_URL",raising=False)
    monkeypatch.delenv("RUNGROW_ADAPTER_DISPATCH_URL_PHOTO_STUDIO_GROWTH_OS",raising=False)
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET","test-adapter-secret")
    tools=WorkforceToolRegistry()
    payload,_,_=tools.execute(name="product_onboarding_trigger",employee=BY_ID["onboarding"],
        principal_permissions=frozenset({"*"}),tenant_id="tenant-w22",
        payload={"product_id":"photo-studio-growth-os","external_tenant_id":"customer-tenant",
            "workforce_tenant_id":"workforce-tenant"})
    engine=create_async_engine(DB_URL,pool_pre_ping=True)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    tenant="w22-onboarding-"+uuid4().hex
    directive_id=str(uuid4()); order_id=str(uuid4())
    founder=Principal("w22-founder",tenant,"founder",frozenset({"*"}),
        product_ids=frozenset({"photo-studio-growth-os"}))
    agent=Principal("onboarding",tenant,"agent",frozenset({"*"}),
        actor_type="agent",product_ids=frozenset({"photo-studio-growth-os"}))
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session,agent) as repo:
                await session.execute(text("""INSERT INTO executive_directives
                  (id,tenant_id,product_id,founder_id,command,status)
                  VALUES (:id,:tenant,'photo-studio-growth-os','w22-founder','Trigger onboarding','DELEGATED')"""),
                  {"id":directive_id,"tenant":tenant})
                await session.execute(text("""INSERT INTO department_work_orders
                  (id,tenant_id,directive_id,product_id,department,department_head_id,title,objective,
                   dependencies,specialist_role_id,tool_name,tool_input,max_budget_cents,spent_cents,status,revision_count)
                  VALUES (:id,:tenant,:directive,'photo-studio-growth-os','Customer Success','cs-director',
                   'Onboarding','Prepare onboarding','[]'::jsonb,'onboarding','product_onboarding_trigger',
                   '{}'::jsonb,100,0,'IN_PROGRESS',0)"""),
                  {"id":order_id,"tenant":tenant,"directive":directive_id})
                approval=await repo.create_approval(product_id="photo-studio-growth-os",
                    employee_id="onboarding",action_type="product.onboarding.dispatch",payload=payload,
                    expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)+__import__("datetime").timedelta(hours=1))
                await session.execute(text("""INSERT INTO wf_outbox_events
                  (id,tenant_id,product_id,directive_id,work_order_id,approval_id,employee_id,
                   action_type,payload_hash,payload,status)
                  VALUES (:id,:tenant,'photo-studio-growth-os',:directive,:order,:approval,
                   'onboarding','product.onboarding.dispatch',:hash,CAST(:outbox AS jsonb),'QUEUED_FOR_APPROVAL')"""),
                  {"id":str(uuid4()),"tenant":tenant,"directive":directive_id,"order":order_id,
                   "approval":approval["id"],"hash":approval["payload_hash"],
                   "outbox":__import__("json").dumps({"channel":"product_adapter","dispatch_payload":payload})})
                repo.principal=founder
                with pytest.raises(Exception) as raised:
                    await approve_single_use_outbox_action(approval["id"],SingleUseApprovalRequest(),founder,repo)
                assert getattr(raised.value,"status_code",None)==503
                outbox=(await session.execute(text("SELECT status FROM wf_outbox_events WHERE approval_id=:id"),
                    {"id":approval["id"]})).scalar_one()
                assert outbox=="BLOCKED_UNCONFIGURED"
                consumed=(await session.execute(text("SELECT consumed_at FROM wf_approvals WHERE id=:id"),
                    {"id":approval["id"]})).scalar_one()
                assert consumed is not None
                audit=(await repo.list_audit("photo-studio-growth-os"))
                assert any(x["action"]=="outbox.adapter_dispatch_blocked_unconfigured" for x in audit)
    finally:
        await engine.dispose()
