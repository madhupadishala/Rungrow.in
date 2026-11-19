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
                ('wf_tasks','wf_agent_runs','wf_memory','wf_approvals','wf_audit_events','wf_model_usage','adapter_idempotency_keys','product_telemetry_events','investment_capital_events','ceo_briefings')
            """))
            rows=list(forced)
            assert len(rows)==10
            assert all(row.relforcerowsecurity for row in rows)
            owners=await conn.execute(text("""
              SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='public' AND c.relname IN
                ('wf_tasks','wf_agent_runs','wf_memory','wf_approvals','wf_audit_events')
                AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
            """))
            assert owners.scalar_one()==0
            delete_privilege=await conn.execute(text("SELECT has_table_privilege(current_user,'wf_audit_events','DELETE')"))
            assert delete_privilege.scalar_one() is False
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_rls_tenant_context_hides_other_tenants():
    engine=create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','tenant-b',true)"))
            await conn.execute(text("INSERT INTO wf_tasks(id,tenant_id,product_id,department,assignee_role_id,title,description,priority,status,created_by,idempotency_key,created_at) VALUES (gen_random_uuid(),'tenant-b',NULL,'Ops','market-intel','B task','','normal','QUEUED','user-b','rls-fixture-b',now()) ON CONFLICT DO NOTHING"))
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
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE wf_audit_events SET action='tampered' WHERE resource_id='audit-fixture'"))
            with pytest.raises(Exception):
                async with conn.begin_nested():
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
                async with conn.begin_nested():
                    await conn.execute(text("""
                      INSERT INTO wf_tasks(id,tenant_id,product_id,department,assignee_role_id,title,description,priority,status,created_by,idempotency_key,created_at)
                      VALUES(gen_random_uuid(),'tenant-a','not-registered','Ops','market-intel','invalid product','','normal','QUEUED','tenant-a','bad-product-fixture',now())
                    """))
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("""
                      INSERT INTO wf_tasks(id,tenant_id,product_id,department,assignee_role_id,title,description,priority,status,created_by,idempotency_key,created_at)
                      VALUES(gen_random_uuid(),'tenant-a','photo-studio-growth-os','Ops','market-intel','unentitled product','','normal','QUEUED','tenant-a','unentitled-fixture',now())
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
    first=FastAPI()
    first.include_router(workforce_app.router)
    second=FastAPI()
    second.include_router(workforce_app.router)
    for instance in (first,second):
        instance.dependency_overrides[current_principal]=principal_override
        instance.dependency_overrides[get_workforce_repo]=repository_override
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
        factory=session_factory()
        await factory.kw["bind"].dispose()
        session_factory.cache_clear()
        settings.database_url=""

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
                    await conn.execute(text("UPDATE wf_approvals SET payload=CAST('{\"message\":\"tampered\"}' AS jsonb) WHERE id=:id"),{"id":str(approval)})
            expired=(await conn.execute(text("""
              INSERT INTO wf_approvals(id,tenant_id,product_id,employee_id,action_type,payload,payload_hash,
                requester_id,approver_id,status,expires_at,decided_at,created_at)
              VALUES(gen_random_uuid(),'approval-tenant','photo-studio-growth-os','employee-1','message.send',
                CAST(:payload AS jsonb),:digest,'requester','approver','APPROVED',now()-interval '1 second',now()-interval '10 minutes',now()-interval '20 minutes') RETURNING id
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
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE wf_approvals SET consumed_at=NULL,execution_run_id=NULL WHERE id=:id"),{"id":str(valid)})
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_model_usage_ledger_is_tenant_product_scoped_and_state_machine_is_enforced():
    engine=create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','usage-tenant-a',true)"))
            await conn.execute(text("SELECT set_config('app.product_ids','photo-studio-growth-os',true)"))
            await conn.execute(text("""
              INSERT INTO wf_model_usage(id,tenant_id,product_id,employee_id,run_id,provider,model,
                prompt_tokens,completion_tokens,estimated_cost_inr,latency_ms)
              VALUES(gen_random_uuid(),'usage-tenant-a','photo-studio-growth-os','market-intel','usage-run',
                'mock','fixture',10,20,0.01,15)
            """))
            task_id=(await conn.execute(text("""
              INSERT INTO wf_tasks(id,tenant_id,product_id,department,assignee_role_id,title,description,
                priority,status,created_by,idempotency_key,created_at)
              VALUES(gen_random_uuid(),'usage-tenant-a','photo-studio-growth-os','Growth','market-intel',
                'state machine','','normal','COMPLETED','tester','terminal-state-fixture',now()) RETURNING id
            """))).scalar_one()
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','usage-tenant-b',true)"))
            await conn.execute(text("SELECT set_config('app.product_ids','photo-studio-growth-os',true)"))
            rows=await conn.execute(text("SELECT * FROM wf_model_usage WHERE tenant_id='usage-tenant-a'"))
            assert list(rows)==[]
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','usage-tenant-a',true)"))
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE wf_tasks SET status='RUNNING' WHERE id=:id"),{"id":str(task_id)})
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_product_telemetry_and_adapter_idempotency_are_forced_and_tenant_product_scoped():
    engine=create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','adapter-tenant-a',true)"))
            await conn.execute(text("SELECT set_config('app.product_ids','photo-studio-growth-os',true)"))
            await conn.execute(text("""
              INSERT INTO product_telemetry_events(id,tenant_id,external_tenant_id,product_id,event_id,event_type,
                recognised_revenue_inr,metadata,correlation_id)
              VALUES(gen_random_uuid(),'adapter-tenant-a','studio-customer-a','photo-studio-growth-os','invoice-event-1',
                'invoice_recognised',125000,'{}'::jsonb,'corr-a')
            """))
            await conn.execute(text("""
              INSERT INTO adapter_idempotency_keys(id,tenant_id,external_tenant_id,product_id,direction,idempotency_key,
                payload_hash,status,correlation_id)
              VALUES(gen_random_uuid(),'adapter-tenant-a','studio-customer-a','photo-studio-growth-os','inbound',
                'webhook-event-1',repeat('a',64),'PROCESSING','corr-a')
            """))
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("""
                      INSERT INTO product_telemetry_events(id,tenant_id,external_tenant_id,product_id,event_id,event_type,
                        recognised_revenue_inr,metadata,correlation_id)
                      VALUES(gen_random_uuid(),'adapter-tenant-a','studio-customer-a','photo-studio-growth-os','bad-revenue',
                        'trial_started',100,'{}'::jsonb,'corr-a')
                    """))
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE product_telemetry_events SET recognised_revenue_inr=0 WHERE event_id='invoice-event-1'"))
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE adapter_idempotency_keys SET payload_hash=repeat('b',64) WHERE idempotency_key='webhook-event-1'"))
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','adapter-tenant-b',true)"))
            await conn.execute(text("SELECT set_config('app.product_ids','photo-studio-growth-os',true)"))
            telemetry=await conn.execute(text("SELECT * FROM product_telemetry_events WHERE tenant_id='adapter-tenant-a'"))
            keys=await conn.execute(text("SELECT * FROM adapter_idempotency_keys WHERE tenant_id='adapter-tenant-a'"))
            assert list(telemetry)==[]
            assert list(keys)==[]
    finally:
        await engine.dispose()



@pytest.mark.asyncio
async def test_capital_and_ceo_ledgers_are_tenant_scoped_and_append_only():
    engine=create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','tenant-finance-a',true)"))
            await conn.execute(text("""
              INSERT INTO investment_capital_events
                (tenant_id,event_type,investor_ref,amount_inr,is_received,occurred_at,created_by)
              VALUES ('tenant-finance-a','investor_lead','investor-fixture',0,false,now(),'founder')
            """))
            await conn.execute(text("""
              INSERT INTO ceo_briefings
                (tenant_id,generated_at,query_started_at,query_completed_at,source_provenance,snapshot,created_by)
              VALUES ('tenant-finance-a',now(),now(),now(),'["wf_tasks"]'::jsonb,CAST(:snapshot AS jsonb),'founder')
            """),{"snapshot":'{"operating_revenue":{"actual_inr":0}}'})
            assert (await conn.execute(text("SELECT count(*) FROM investment_capital_events"))).scalar_one()==1
            assert (await conn.execute(text("SELECT count(*) FROM ceo_briefings"))).scalar_one()==1
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE investment_capital_events SET amount_inr=100 WHERE investor_ref='investor-fixture'"))
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("DELETE FROM investment_capital_events WHERE investor_ref='investor-fixture'"))
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE ceo_briefings SET snapshot='{}'::jsonb"))
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("DELETE FROM ceo_briefings"))
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','tenant-finance-b',true)"))
            assert (await conn.execute(text("SELECT count(*) FROM investment_capital_events"))).scalar_one()==0
            assert (await conn.execute(text("SELECT count(*) FROM ceo_briefings"))).scalar_one()==0
    finally:
        await engine.dispose()



@pytest.mark.asyncio
async def test_capital_event_requires_matching_approval_and_consumes_it_once():
    import json
    engine=create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.current_tenant_id','tenant-capital-test',true)"))
            payload={"event_type":"capital_received","investor_ref":"INV-TEST-01","amount_inr":250000}
            approval_id=(await conn.execute(text("""
              INSERT INTO wf_approvals
                (id,tenant_id,product_id,employee_id,action_type,payload,payload_hash,
                 requester_id,approver_id,status,expires_at,decided_at,created_at)
              VALUES (gen_random_uuid(),'tenant-capital-test',NULL,'finance','investment.commit',
                CAST(:payload AS jsonb),:hash,'finance-user','founder-user','APPROVED',
                now()+interval '1 hour',now(),now()) RETURNING id
            """),{"payload":json.dumps(payload),"hash":"0"*64})).scalar_one()
            await conn.execute(text("""
              INSERT INTO investment_capital_events
                (tenant_id,event_type,investor_ref,amount_inr,is_received,approval_id,occurred_at,created_by)
              VALUES ('tenant-capital-test','capital_received','INV-TEST-01',250000,true,:approval,now(),'finance-user')
            """),{"approval":approval_id})
            consumed=(await conn.execute(text("SELECT consumed_at FROM wf_approvals WHERE id=:id"),{"id":approval_id})).scalar_one()
            assert consumed is not None
            with pytest.raises(Exception):
                async with conn.begin_nested():
                    await conn.execute(text("""
                      INSERT INTO investment_capital_events
                        (tenant_id,event_type,investor_ref,amount_inr,is_received,approval_id,occurred_at,created_by)
                      VALUES ('tenant-capital-test','capital_received','INV-TEST-01',250000,true,:approval,now(),'finance-user')
                    """),{"approval":approval_id})
    finally:
        await engine.dispose()
