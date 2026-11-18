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
            with pytest.raises(Exception):
                await conn.execute(text("DELETE FROM wf_audit_events WHERE resource_id='audit-fixture'"))
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
    from app.repositories import PostgresWorkforceRepository, session_factory
    from app.security import current_principal
    from app.config import settings
    settings.database_url=URL
    session_factory.cache_clear()

    principal=Principal("durability-user","durability-tenant","founder",frozenset({"*"}),
                        product_ids=frozenset({"photo-studio-growth-os"}))
    async def principal_override():
        return principal
    async def repository_override(p=Depends(current_principal)):
        factory=session_factory()
        async with factory() as session:
            async with PostgresWorkforceRepository(session,p) as repo:
                yield repo
    for instance in (first,second):
        instance.dependency_overrides[current_principal]=principal_override
        instance.dependency_overrides[get_workforce_repo]=repository_override
    first=FastAPI()
    first.include_router(workforce_app.router)
    second=FastAPI()
    second.include_router(workforce_app.router)
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
        first.dependency_overrides.clear()
        second.dependency_overrides.clear()
        session_factory.cache_clear()

@pytest.mark.asyncio
async def test_task_lease_recovery_and_dead_letter_state_machine():
    engine=create_async_engine(URL)
    task_id=None
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','lease-tenant',true)"))
            await conn.execute(text("SELECT set_config('app.product_ids','photo-studio-growth-os',true)"))
            row=(await conn.execute(text("""
              INSERT INTO wf_tasks(id,tenant_id,product_id,department,assignee_role_id,title,description,priority,status,
                created_by,idempotency_key,created_at,attempt_count,max_attempts,lease_expires_at)
              VALUES(gen_random_uuid(),'lease-tenant','photo-studio-growth-os','Growth','market-intel','lease test','','normal',
                'RUNNING','lease-user','lease-recovery-once',now(),1,3,now()-interval '5 seconds')
              RETURNING id
            """))).one()
            task_id=str(row.id)
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','lease-tenant',true)"))
            await conn.execute(text("SELECT set_config('app.product_ids','photo-studio-growth-os',true)"))
            row=(await conn.execute(text("""
              WITH candidate AS (
                SELECT id FROM wf_tasks WHERE id=:id AND status='RUNNING' AND lease_expires_at<=now()
                FOR UPDATE SKIP LOCKED LIMIT 1
              )
              UPDATE wf_tasks t SET status='RUNNING',locked_by='worker-two',
                lease_expires_at=now()+interval '30 seconds',attempt_count=attempt_count+1
              FROM candidate c WHERE t.id=c.id RETURNING t.id,t.attempt_count,t.locked_by
            """),{"id":task_id})).one()
            assert row.attempt_count==2 and row.locked_by=="worker-two"
            completed=await conn.execute(text("""
              UPDATE wf_tasks SET status='COMPLETED',locked_by=NULL,lease_expires_at=NULL
              WHERE id=:id AND status='RUNNING' AND locked_by='worker-two' AND lease_expires_at>now()
            """),{"id":task_id})
            assert completed.rowcount==1
            duplicate=await conn.execute(text("""
              INSERT INTO wf_tasks(id,tenant_id,product_id,department,assignee_role_id,title,description,priority,status,
                created_by,idempotency_key,created_at)
              VALUES(gen_random_uuid(),'lease-tenant','photo-studio-growth-os','Growth','market-intel','duplicate','','normal',
                'QUEUED','lease-user','lease-recovery-once',now()) ON CONFLICT(tenant_id,idempotency_key) DO NOTHING
            """))
            assert duplicate.rowcount==0
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','lease-tenant',true)"))
            await conn.execute(text("SELECT set_config('app.product_ids','photo-studio-growth-os',true)"))
            dead=(await conn.execute(text("""
              INSERT INTO wf_tasks(id,tenant_id,product_id,department,assignee_role_id,title,description,priority,status,
                created_by,idempotency_key,created_at,attempt_count,max_attempts,lease_expires_at)
              VALUES(gen_random_uuid(),'lease-tenant','photo-studio-growth-os','Growth','market-intel','dead letter','','normal',
                'RUNNING','lease-user','lease-dead-letter',now(),3,3,now()-interval '5 seconds') RETURNING id
            """))).scalar_one()
            moved=await conn.execute(text("""
              UPDATE wf_tasks SET status='DEAD_LETTER',locked_by=NULL,lease_expires_at=NULL
              WHERE id=:id AND status='RUNNING' AND lease_expires_at<=now() AND attempt_count>=max_attempts
            """),{"id":str(dead)})
            assert moved.rowcount==1
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_approval_single_use_and_payload_immutability_are_database_enforced():
    engine=create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','approval-tenant',true)"))
            await conn.execute(text("SELECT set_config('app.product_ids','photo-studio-growth-os',true)"))
            payload='{"message":"hello"}'
            digest=__import__("hashlib").sha256(b'{"action_type":"message.send","employee_id":"employee-1","payload":{"message":"hello"},"product_id":"photo-studio-growth-os","tenant_id":"approval-tenant"}').hexdigest()
            approval=(await conn.execute(text("""
              INSERT INTO wf_approvals(id,tenant_id,product_id,employee_id,action_type,payload,payload_hash,
                requester_id,status,expires_at,created_at)
              VALUES(gen_random_uuid(),'approval-tenant','photo-studio-growth-os','employee-1','message.send',
                CAST(:payload AS jsonb),:digest,'requester','PENDING',now()+interval '5 minutes',now()) RETURNING id
            """),{"payload":payload,"digest":digest})).scalar_one()
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE wf_approvals SET status='APPROVED',approver_id=requester_id,decided_at=now() WHERE id=:id"),{"id":str(approval)})
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE wf_approvals SET payload=CAST('{\\"message\\":\\"tampered\\"}' AS jsonb) WHERE id=:id"),{"id":str(approval)})
            expired=(await conn.execute(text("""
              INSERT INTO wf_approvals(id,tenant_id,product_id,employee_id,action_type,payload,payload_hash,
                requester_id,approver_id,status,expires_at,decided_at,created_at)
              VALUES(gen_random_uuid(),'approval-tenant','photo-studio-growth-os','employee-1','message.send',
                CAST(:payload AS jsonb),:digest,'requester','approver','APPROVED',now()-interval '1 second',now(),now()) RETURNING id
            """),{"payload":payload,"digest":digest})).scalar_one()
            rejected=await conn.execute(text("""
              UPDATE wf_approvals SET consumed_at=now(),execution_run_id=gen_random_uuid()
              WHERE id=:id AND status='APPROVED' AND expires_at>now() AND consumed_at IS NULL
            """),{"id":str(expired)})
            assert rejected.rowcount==0
            valid=(await conn.execute(text("""
              INSERT INTO wf_approvals(id,tenant_id,product_id,employee_id,action_type,payload,payload_hash,
                requester_id,approver_id,status,expires_at,decided_at,created_at)
              VALUES(gen_random_uuid(),'approval-tenant','photo-studio-growth-os','employee-1','message.send',
                CAST(:payload AS jsonb),:digest,'requester','approver','APPROVED',now()+interval '5 minutes',now(),now()) RETURNING id
            """),{"payload":payload,"digest":digest})).scalar_one()
            consumed=await conn.execute(text("""
              UPDATE wf_approvals SET consumed_at=now(),execution_run_id=gen_random_uuid()
              WHERE id=:id AND status='APPROVED' AND expires_at>now() AND consumed_at IS NULL
            """),{"id":str(valid)})
            assert consumed.rowcount==1
            replay=await conn.execute(text("""
              UPDATE wf_approvals SET consumed_at=now(),execution_run_id=gen_random_uuid()
              WHERE id=:id AND status='APPROVED' AND expires_at>now() AND consumed_at IS NULL
            """),{"id":str(valid)})
            assert replay.rowcount==0
    finally:
        await engine.dispose()
