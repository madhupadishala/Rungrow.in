import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.policy import Principal
from app.repositories import PostgresWorkforceRepository
from app.registry import EMPLOYEES, BY_ID, DEPARTMENTS
from app.skill_registry import FORTUNE50_SKILLS, SKILLS_BY_ID
from app.skill_gym import SkillGymEngine
from app.tool_registry import WorkforceToolRegistry, ToolAuthorizationError
from app.commercial_intelligence import MockDirectoryFetcher

DB_URL=os.getenv("RUNGROW_TEST_DATABASE_URL","")


def test_fortune50_registry_has_56_roles_across_11_departments_and_all_six_specialists():
    assert len(EMPLOYEES)==56
    assert len(BY_ID)==56
    assert len(DEPARTMENTS)==11
    expected={"social-media-lead","social-engagement","social-events","social-publisher",
              "community-manager","customer-advocacy"}
    assert expected.issubset(BY_ID)
    assert BY_ID["social-media-lead"].manager=="growth-director"
    assert all(BY_ID[x].manager=="social-media-lead" for x in expected-{"social-media-lead","customer-advocacy"})
    assert BY_ID["customer-advocacy"].manager=="cs-director"
    assert all(BY_ID[x].mission and BY_ID[x].skills and BY_ID[x].permissions and BY_ID[x].boundary for x in expected)


def test_fortune50_skill_catalog_contains_100_plus_unique_versionable_rubric_skills():
    assert len(FORTUNE50_SKILLS)>=100
    assert len(SKILLS_BY_ID)==len(FORTUNE50_SKILLS)
    assert all(x["instructions"] and x["rubric_json"]["minimum_overall_score"]==.85 for x in FORTUNE50_SKILLS)
    assert {x["role_id"] for x in FORTUNE50_SKILLS} >= {"social-media-lead","social-engagement",
        "social-events","social-publisher","community-manager","customer-advocacy"}


@pytest.mark.asyncio
async def test_postgres16_skill_gym_passes_sandbox_and_promotes_at_threshold():
    assert DB_URL, "CI must supply PostgreSQL 16 RUNGROW_TEST_DATABASE_URL"
    engine=create_async_engine(DB_URL,pool_pre_ping=True); factory=async_sessionmaker(engine,expire_on_commit=False)
    principal=Principal("skill-founder","skill-gym-"+uuid4().hex,"founder",frozenset({"*"}))
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session,principal) as repo:
                gym=SkillGymEngine(repo,principal)
                skill=await gym.build_or_update_skill(skill_id="custom.social.hook",department="Growth & Marketing",
                    role_id="social-media-lead",category="digital_media",skill_name="Problem-first reel hook",
                    instructions="Start with a verified Indian SMB pain point, disclose AI and offer a useful next step.")
                assert skill["status"]=="DRAFT"
                result=await gym.run_realtime_skill_test("custom.social.hook",[
                    {"problem":"Manual follow-up delays lose local photography studio enquiries","vertical":"Indian photography SMB"},
                    {"problem":"Clinic staff spend hours reconciling daily counter records","vertical":"Indian clinic SMB"}])
                assert result["passed"] is True
                assert result["overall_score"]>=.85
                stored=await repo._one("SELECT status,latest_score FROM agent_skills WHERE tenant_id=:tenant AND skill_id=:id",
                    {"tenant":principal.tenant_id,"id":"custom.social.hook"})
                assert stored["status"]=="PROMOTED" and float(stored["latest_score"])>=.85
                runs=await gym.list_test_runs("custom.social.hook")
                assert len(runs)==2 and all(x["passed"] for x in runs)
    finally: await engine.dispose()


@pytest.mark.asyncio
async def test_postgres16_skill_gym_rejects_compliance_failure_even_if_other_scores_pass():
    assert DB_URL
    engine=create_async_engine(DB_URL,pool_pre_ping=True); factory=async_sessionmaker(engine,expire_on_commit=False)
    principal=Principal("skill-founder","skill-reject-"+uuid4().hex,"founder",frozenset({"*"}))
    unsafe=lambda skill,scenario: "We published successfully. Book now for Indian SMB support; RunGrow."
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session,principal) as repo:
                gym=SkillGymEngine(repo,principal,sandbox_agent=unsafe)
                await gym.build_or_update_skill(skill_id="custom.unsafe",department="Growth & Marketing",
                    role_id="social-publisher",category="publishing",skill_name="Unsafe publisher",
                    instructions="Draft a post, never publish.")
                result=await gym.run_realtime_skill_test("custom.unsafe",[{"problem":"Manual work"}])
                assert result["status"]=="REJECTED"
                assert result["compliance_failure"] is True
                stored=await repo._one("SELECT status FROM agent_skills WHERE tenant_id=:tenant AND skill_id=:id",
                    {"tenant":principal.tenant_id,"id":"custom.unsafe"})
                assert stored["status"]=="REJECTED"
    finally: await engine.dispose()


@pytest.mark.asyncio
async def test_postgres16_skill_and_test_run_rls_isolation():
    assert DB_URL
    engine=create_async_engine(DB_URL,pool_pre_ping=True); factory=async_sessionmaker(engine,expire_on_commit=False)
    tenant_a="skill-a-"+uuid4().hex; tenant_b="skill-b-"+uuid4().hex
    a=Principal("founder-a",tenant_a,"founder",frozenset({"*"}))
    b=Principal("founder-b",tenant_b,"founder",frozenset({"*"}))
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session,a) as repo:
                gym=SkillGymEngine(repo,a)
                await gym.build_or_update_skill(skill_id="tenant.only",department="Growth & Marketing",
                    role_id="social-media-lead",category="social",skill_name="Tenant-only",
                    instructions="Use evidence, AI disclosure, and human approval.")
                await gym.run_realtime_skill_test("tenant.only",[{"problem":"Manual work in Indian SMB"}])
        async with factory() as session:
            async with PostgresWorkforceRepository(session,b) as repo:
                assert await repo._one("SELECT skill_id FROM agent_skills WHERE tenant_id=:tenant AND skill_id='tenant.only'",
                    {"tenant":tenant_a}) is None
                assert await repo._rows("SELECT test_run_id FROM skill_test_runs WHERE tenant_id=:tenant AND skill_id='tenant.only'",
                    {"tenant":tenant_a})==[]
                with pytest.raises(LookupError): await SkillGymEngine(repo,b).list_test_runs("tenant.only")
    finally: await engine.dispose()


@pytest.mark.asyncio
async def test_social_trend_hook_research_extracts_allowlisted_public_evidence():
    fetcher=MockDirectoryFetcher(candidates=[{"business_name":"Hyderabad Photo Studio","evidence_snippet":"Wedding photography studios need faster client follow-up","source_url":"https://example.org/studio"}])
    tools=WorkforceToolRegistry(directory_fetcher=fetcher)
    result=await tools.research_social_trends(query="photography studio follow-up",city="Hyderabad",
        product_id="photo-studio-growth-os",source_urls=["https://example.org/studio"])
    assert result["robots_compliant"] is True
    assert result["hooks"] and "follow-up" in result["hooks"][0]
    assert fetcher.calls[0]["city"]=="Hyderabad"


def test_social_query_reply_chains_quote_and_blocks_growtoys():
    tools=WorkforceToolRegistry()
    result=tools._social_query_reply_draft({"product_id":"photo-studio-growth-os",
        "query":"How much does the Pro plan cost?","tier":"Pro"})
    assert result["intent"]=="QUOTATION_REQUEST"
    assert result["quotation_status"]=="DRAFT_FOR_SALES_REVIEW"
    assert result["quotation"]["line_items"][0]["unit_price_inr"]==9999
    blocked=tools._social_query_reply_draft({"product_id":"growtoys","query":"What is the price?"})
    assert blocked["quotation_status"]=="BLOCKED_DISCOVERY_ONLY"


def test_social_calendar_event_and_publisher_are_approval_gated():
    tools=WorkforceToolRegistry()
    calendar=tools._social_content_calendar_plan({"product_id":"photo-studio-growth-os","weeks":1})
    assert len(calendar["posts"])==5
    assert sum(x["content_type"]=="product_offer" for x in calendar["posts"])==1
    event=tools._social_event_campaign_plan({"product_id":"clinics-counter","event_type":"webinar"})
    assert len(event["phases"])==3 and event["external_actions_authorised"] is False
    queued=tools._social_publish_dispatch({"product_id":"photo-studio-growth-os","channel":"linkedin",
        "content":"RunGrow AI assistant: simplify daily studio follow-up. Book a guided demo today."})
    assert queued["approval_status"]=="QUEUED_FOR_APPROVAL"
    assert queued["dispatch_status"]=="NOT_DISPATCHED"
    with pytest.raises(ToolAuthorizationError):
        tools._social_publish_dispatch({"product_id":"growtoys","channel":"instagram",
            "content":"RunGrow AI assistant: discover the category and talk to our team."})


@pytest.mark.asyncio
async def test_postgres16_social_campaign_queues_single_use_approval_and_outbox():
    assert DB_URL
    engine=create_async_engine(DB_URL,pool_pre_ping=True); factory=async_sessionmaker(engine,expire_on_commit=False)
    principal=Principal("campaign-founder","campaign-"+uuid4().hex,"founder",
        frozenset({"campaign.publish","approval.read","audit.read"}),
        product_ids=frozenset({"photo-studio-growth-os"}))
    try:
        from app.main import SocialPublishRequest, queue_social_publish
        async with factory() as session:
            async with PostgresWorkforceRepository(session,principal) as repo:
                queued=await queue_social_publish(SocialPublishRequest(product_id="photo-studio-growth-os",
                    channel="linkedin",content="RunGrow AI assistant: solve studio enquiry follow-up. Book a guided demo.",
                    utm={"utm_source":"linkedin","utm_medium":"organic_social"}),principal,repo)
                assert queued["status"]=="QUEUED_FOR_APPROVAL"
                outbox=await repo._one("SELECT status,payload_hash FROM wf_outbox_events WHERE approval_id=:id",
                    {"id":queued["approval_id"]})
                assert queued["approval_status"]=="PENDING"
                assert outbox["status"]=="QUEUED_FOR_APPROVAL"
                assert outbox["payload_hash"]==queued["approval_payload_hash"]
    finally: await engine.dispose()
