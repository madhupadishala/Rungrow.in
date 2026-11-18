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
            role=(await conn.execute(text("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user"))).one()
            assert role == (False,False,False)
            forced=await conn.execute(text("""
              SELECT c.relname,c.relforcerowsecurity
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='public' AND c.relname IN
                ('wf_tasks','wf_agent_runs','wf_memory','wf_approvals','wf_audit_events')
            """))
            rows=list(forced)
            assert len(rows)==5
            assert all(row.relforcerowsecurity for row in rows)
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
