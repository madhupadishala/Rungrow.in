from __future__ import annotations
import os
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import asyncpg
import httpx
import pytest
from app.commercial_intelligence import CommercialIntelligenceService, MockDirectoryFetcher, RobotsCompliantHttpFetcher
from app.commercial_qualification import FixtureDiscoveryProvider, ProspectQualificationService
from app.policy import Principal, evaluate
from app.repositories import InMemoryWorkforceRepository


PRODUCT="photo-studio-growth-os"
TENANT="commercial-ci"
def make_repo():
    p=Principal("founder",TENANT,"founder",frozenset({"*"}),product_ids=frozenset({PRODUCT}))
    return InMemoryWorkforceRepository(p,{PRODUCT})


def fixture_candidate():
    return {"business_name":"Dilsukhnagar Wedding Photography Studio","city":"Hyderabad",
        "segment":"wedding photography studio","source_url":"https://approved.example/dilsukhnagar/studio",
        "evidence_urls":["https://approved.example/dilsukhnagar/studio"],
        "evidence_snippet":"Dilsukhnagar wedding photography studio offering wedding albums and photography.",
        "raw_evidence_snippet":"Dilsukhnagar wedding photography studio offering wedding albums and photography.",
        "fetched_at":"2026-11-24T08:00:00+00:00","verified_business_identity":True,
        "active_public_presence":True}


def test_domain_allowlist_is_fail_closed_and_exact():
    closed=RobotsCompliantHttpFetcher(transport=httpx.MockTransport(lambda r:httpx.Response(404,request=r)))
    with pytest.raises(PermissionError,match="allowlist is empty"):
        closed._validate_url("https://unapproved.example/listing")
    allowed=RobotsCompliantHttpFetcher(allow_domains={"approved.example"},transport=httpx.MockTransport(lambda r:httpx.Response(404,request=r)))
    with pytest.raises(PermissionError,match="allow list"):
        allowed._validate_url("https://approved.example.evil.test/listing")
    assert allowed._validate_url("https://approved.example/listing")[1]=="approved.example"


@pytest.mark.asyncio
async def test_fixture_discovery_ingests_exact_source_timestamp_and_raw_evidence_and_dedupes():
    repo=make_repo()
    candidate=fixture_candidate()
    fixture=MockDirectoryFetcher([candidate])
    service=CommercialIntelligenceService(repo,fixture)
    first=await service.discover(tenant_id=TENANT,product_id=PRODUCT,query="wedding photography",city="Hyderabad")
    second=await service.discover(tenant_id=TENANT,product_id=PRODUCT,query="wedding photography",city="Hyderabad")
    rows=await repo.list_prospects(PRODUCT)
    assert first["upserted"]==second["upserted"]==1
    assert len(rows)==1 and rows[0]["source_url"]==candidate["source_url"]
    assert rows[0]["metadata"]["discovered_at"]==candidate["fetched_at"]
    assert rows[0]["metadata"]["raw_evidence_snippet"]==candidate["raw_evidence_snippet"]
    assert rows[0]["evidence_urls"]==[candidate["source_url"]]


@pytest.mark.asyncio
async def test_revalidation_qualifies_verified_locality_and_rejects_incomplete_historical_record():
    repo=make_repo()
    candidate=fixture_candidate()
    await repo.upsert_prospect(product_id=PRODUCT,dedupe_key="valid",business_name=candidate["business_name"],
        city="Hyderabad",segment=candidate["segment"],decision_maker_name=None,public_contact_channel=None,
        source_url=candidate["source_url"],evidence_urls=[candidate["source_url"]],
        evidence_snippet=candidate["evidence_snippet"],confidence_score=.9,icp_fit_tier="HIGH",
        is_synthetic=False,metadata={})
    await repo.upsert_prospect(product_id=PRODUCT,dedupe_key="historical",business_name="Old Studio",
        city="Hyderabad",segment="photo studio",decision_maker_name=None,public_contact_channel=None,
        source_url="https://old.example/removed",evidence_urls=[],evidence_snippet="",
        confidence_score=.2,icp_fit_tier="UNASSESSED",is_synthetic=False,metadata={})
    provider=FixtureDiscoveryProvider([candidate])
    result=await ProspectQualificationService(repo,provider).revalidate(product_id=PRODUCT)
    assert result["checked"]==2 and result["qualified"]==1 and result["unverified"]==1
    rows=await repo.list_prospects(PRODUCT)
    valid=next(x for x in rows if x["dedupe_key"]=="valid")
    invalid=next(x for x in rows if x["dedupe_key"]=="historical")
    assert valid["stage"]=="QUALIFIED"
    assert valid["metadata"]["qualification"]["locality"]=="Dilsukhnagar"
    assert invalid["stage"]=="DISCOVERED"
    assert invalid["metadata"]["revalidation"]["status"]=="UNVERIFIED"


@pytest.mark.asyncio
async def test_sales_artifacts_are_persisted_as_pending_human_review_only():
    repo=make_repo()
    candidate=fixture_candidate()
    prospect=await repo.upsert_prospect(product_id=PRODUCT,dedupe_key="qualified",business_name=candidate["business_name"],
        city="Hyderabad",segment=candidate["segment"],decision_maker_name=None,public_contact_channel=None,
        source_url=candidate["source_url"],evidence_urls=[candidate["source_url"]],
        evidence_snippet=candidate["evidence_snippet"],confidence_score=.9,icp_fit_tier="HIGH",
        is_synthetic=False,metadata={"qualification":{"status":"QUALIFIED","score":1.0,
        "verified_business_identity":True,"active_public_presence":True,"reasons":["verified"]}})
    prospect=await repo.update_prospect_qualification(prospect_id=prospect["id"],product_id=PRODUCT,
        stage="QUALIFIED",metadata={"qualification":{"status":"QUALIFIED","score":1.0,
        "verified_business_identity":True,"active_public_presence":True,"reasons":["verified"]}})
    artifacts=await ProspectQualificationService(repo,FixtureDiscoveryProvider([candidate])).create_artifacts(
        prospect_id=prospect["id"],product_id=PRODUCT,created_by="founder")
    assert len(artifacts)==3
    assert {x["status"] for x in artifacts}=={"PENDING_HUMAN_APPROVAL"}
    assert all(x["content"]["human_review_required"] and x["content"]["outbound_dispatch"].startswith("BLOCKED") for x in artifacts)
    assert len(await repo.list_sales_artifacts(product_id=PRODUCT,prospect_id=prospect["id"]))==3


def test_unapproved_whatsapp_email_permissions_are_denied_in_controlled_mode():
    p=Principal("agent","tenant-x","agent",frozenset({"whatsapp.send","email.send"}),actor_type="agent")
    assert evaluate(p,"whatsapp.send").allowed is False and evaluate(p,"whatsapp.send").requires_approval
    assert evaluate(p,"email.send").allowed is False and evaluate(p,"email.send").requires_approval


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"),reason="requires PostgreSQL 16 TEST_DATABASE_URL")
@pytest.mark.asyncio
async def test_postgres_16_commercial_schema_persists_qualified_prospect_and_review_artifact():
    raw=os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://","postgresql://",1)
    parts=urlsplit(raw); params=parse_qsl(parts.query)
    sslmode=next((v for k,v in params if k=="sslmode"),None)
    query=urlencode([(k,v) for k,v in params if k not in {"sslmode","channel_binding"}])
    dsn=urlunsplit((parts.scheme,parts.netloc,parts.path,query,parts.fragment))
    schema="commercial_it_"+uuid.uuid4().hex[:12]
    admin=await asyncpg.connect(dsn,ssl="require" if sslmode=="require" else None)
    migration_dir=Path(__file__).resolve().parents[1]/"migrations"
    try:
        await admin.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        await admin.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}", public')
        for filename in ("003_durable_workforce_repositories.sql","007_commercial_intelligence_and_portfolio.sql","015_commercial_qualification_artifacts.sql"):
            sql=(migration_dir/filename).read_text(encoding="utf-8")
            if filename=="007_commercial_intelligence_and_portfolio.sql":
                sql=sql.replace("WHERE conname='prospect_records_confidence_bounds'","WHERE conname='prospect_records_confidence_bounds' AND conrelid='prospect_records'::regclass")
            await admin.execute(sql)
        prospect_id=uuid.uuid4(); tenant="pg-commercial-test"; url="https://approved.example/dilsukhnagar/studio"
        await admin.execute("""INSERT INTO prospect_records
          (id,tenant_id,product_id,dedupe_key,business_name,city,segment,source_url,evidence_urls,evidence_snippet,
           confidence_score,icp_fit_tier,stage,is_synthetic,metadata,qualified_at,qualification_score)
          VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,'QUALIFIED',false,$13::jsonb,now(),1.0)""",
          prospect_id,tenant,PRODUCT,"pg-dedupe","Dilsukhnagar Wedding Photography Studio","Hyderabad","wedding photography",
          url,json.dumps([url]),"Public evidence snippet",.9,"HIGH",json.dumps({"qualification":{"status":"QUALIFIED","score":1.0}}))
        artifact=await admin.fetchrow("""INSERT INTO sales_artifacts
          (tenant_id,product_id,prospect_id,artifact_type,channel,title,content,status,created_by,metadata)
          VALUES ($1,$2,$3,'WHATSAPP_DRAFT','WHATSAPP','Review draft',$4::jsonb,'PENDING_HUMAN_APPROVAL','founder',$5::jsonb)
          RETURNING status,content""",tenant,PRODUCT,prospect_id,json.dumps({"human_review_required":True,"outbound_dispatch":"BLOCKED"}),json.dumps({"source_url":url}))
        assert artifact["status"]=="PENDING_HUMAN_APPROVAL"
        assert json.loads(artifact["content"])["outbound_dispatch"]=="BLOCKED"
    finally:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
