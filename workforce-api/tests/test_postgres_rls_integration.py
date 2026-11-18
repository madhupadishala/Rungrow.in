import os
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

URL=os.getenv("RUNGROW_TEST_DATABASE_URL")
pytestmark=pytest.mark.skipif(not URL,reason="RUNGROW_TEST_DATABASE_URL not configured")

@pytest.mark.asyncio
async def test_postgres_role_is_restricted_and_rls_is_forced():
    engine=create_async_engine(URL)
    try:
        async with engine.connect() as conn:
            role=(await conn.execute(text("SELECT current_user, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user"))).one()
            assert role == ("rungrow_app_user",False,False,False)
            forced=await conn.execute(text("""
              SELECT c.relname,c.relforcerowsecurity
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='public' AND c.relname IN
                ('wf_tasks','wf_agent_runs','wf_memory','wf_approvals','wf_audit_events')
            """))
            rows=list(forced)
            assert len(rows)==5
            assert all(row.relforcerowsecurity for row in rows)
            owners=await conn.execute(text("""
              SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='public' AND c.relname IN
                ('wf_tasks','wf_agent_runs','wf_memory','wf_approvals','wf_audit_events')
                AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
            """))
            assert owners.scalar_one()==0
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_rls_tenant_context_hides_other_tenants():
    engine=create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','tenant-b',true)"))
            await conn.execute(text("INSERT INTO wf_tasks(id,tenant_id,product_id,department,assignee_role_id,title,description,priority,status,created_by,idempotency_key,created_at) VALUES (gen_random_uuid(),'tenant-b',NULL,'Ops','market-intel','B task','','normal','queued','user-b','rls-fixture-b',now()) ON CONFLICT DO NOTHING"))
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','tenant-a',true)"))
            rows=await conn.execute(text("SELECT * FROM wf_tasks WHERE tenant_id='tenant-b'"))
            assert list(rows)==[]
            changed=await conn.execute(text("UPDATE wf_tasks SET title='tampered' WHERE tenant_id='tenant-b'"))
            assert changed.rowcount==0
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_audit_is_append_only_for_restricted_role():
    engine=create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','tenant-a',true)"))
            await conn.execute(text("""
              INSERT INTO wf_audit_events(id,tenant_id,actor_id,action,resource_type,resource_id,details,occurred_at)
              VALUES(gen_random_uuid(),'tenant-a','tester','fixture','test','audit-fixture','{}'::jsonb,now())
            """))
            with pytest.raises(Exception):
                await conn.execute(text("UPDATE wf_audit_events SET action='tampered' WHERE resource_id='audit-fixture'"))
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_unknown_product_foreign_key_and_unentitled_product_rls():
    engine=create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','tenant-a',true)"))
            await conn.execute(text("SELECT set_config('app.product_ids','photo-studio-billing',true)"))
            with pytest.raises(Exception):
                await conn.execute(text("""
                  INSERT INTO wf_tasks(id,tenant_id,product_id,department,assignee_role_id,title,description,priority,status,created_by,idempotency_key,created_at)
                  VALUES(gen_random_uuid(),'tenant-a','not-registered','Ops','market-intel','invalid product','','normal','queued','tenant-a','bad-product-fixture',now())
                """))
            with pytest.raises(Exception):
                await conn.execute(text("""
                  INSERT INTO wf_tasks(id,tenant_id,product_id,department,assignee_role_id,title,description,priority,status,created_by,idempotency_key,created_at)
                  VALUES(gen_random_uuid(),'tenant-a','photo-studio-growth-os','Ops','market-intel','unentitled product','','normal','queued','tenant-a','unentitled-fixture',now())
                """))
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','tenant-a',true)"))
            await conn.execute(text("SELECT set_config('app.product_ids','photo-studio-billing',true)"))
            rows=await conn.execute(text("SELECT * FROM wf_tasks WHERE product_id='photo-studio-growth-os'"))
            assert list(rows)==[]
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_fastapi_records_survive_new_app_instance():
    from fastapi import FastAPI, Depends
    from httpx import ASGITransport, AsyncClient
    from app.main import app as workforce_app, get_workforce_repo
    from app.policy import Principal
    from app.repositories import TenantRepository, session_factory
    from app.security import current_principal

    principal=Principal("durability-user","durability-tenant","founder",frozenset({"*"}),
                        product_ids=frozenset({"photo-studio-growth-os"}))
    async def principal_override():
        return principal
    async def repository_override(p=Depends(current_principal)):
        factory=session_factory()
        async with factory() as session:
            async with TenantRepository(session,p) as repo:
                yield repo
    workforce_app.dependency_overrides[current_principal]=principal_override
    workforce_app.dependency_overrides[get_workforce_repo]=repository_override
    first=FastAPI()
    first.mount("/",workforce_app)
    second=FastAPI()
    second.mount("/",workforce_app)
    try:
        async with AsyncClient(transport=ASGITransport(app=first),base_url="http://first") as client1:
            task=await client1.post("/v1/workforce/tasks",headers={"Idempotency-Key":"durability-task-1"},
                json={"title":"Durability task","assignee_role_id":"market-intel","product_id":"photo-studio-growth-os"})
            assert task.status_code==200,task.text
            memory=await client1.post("/v1/workforce/memory",json={"role_id":"market-intel","product_id":"photo-studio-growth-os","content":"durability memory","source_ref":"integration","retention_days":30})
            assert memory.status_code==200,memory.text
            run=await client1.post("/v1/workforce/agent-runs",json={"role_id":"market-intel","product_id":"photo-studio-growth-os","task":"Research public photo studio market"})
            assert run.status_code==200,run.text
            approval=await client1.post("/v1/workforce/commands",json={"command":"Send campaign to approved contacts","product_id":"photo-studio-growth-os"})
            assert approval.status_code==200 and approval.json()["approval_id"]
        async with AsyncClient(transport=ASGITransport(app=second),base_url="http://second") as client2:
            assert any(x["id"]==task.json()["id"] for x in (await client2.get("/v1/workforce/tasks")).json())
            assert any(x["id"]==memory.json()["id"] for x in (await client2.get("/v1/workforce/memory")).json())
            assert any(x["id"]==run.json()["id"] for x in (await client2.get("/v1/workforce/agent-runs")).json())
            approvals=(await client2.get("/v1/workforce/approvals")).json()
            assert any(x["id"]==approval.json()["approval_id"] for x in approvals)
    finally:
        workforce_app.dependency_overrides.clear()
