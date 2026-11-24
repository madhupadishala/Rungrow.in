from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.departmental_worker import DepartmentalWorker
from app.orchestration import (
    WorkforceOrchestrator, PermanentExecutionError, TransientExecutionError,
    classify_execution_error,
)


@pytest.fixture
def worker(monkeypatch):
    tenant = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setenv("RUNGROW_INTERNAL_TENANT_ID", tenant)
    return DepartmentalWorker(tenant_id=tenant)


def test_worker_identity_is_unique_and_tenant_scoped(worker):
    other = DepartmentalWorker(tenant_id=worker.tenant_id)
    assert worker.worker_id != other.worker_id
    assert worker._principal().tenant_id == worker.tenant_id
    assert worker._principal().actor_type == "service"


def test_stop_is_idempotent(worker):
    worker.stop()
    worker.stop()
    assert worker._stop.is_set()


def test_error_classifier_separates_transient_and_permanent():
    assert isinstance(classify_execution_error(TimeoutError("timeout")), TransientExecutionError)
    assert isinstance(classify_execution_error(PermissionError("denied")), PermanentExecutionError)
    assert isinstance(classify_execution_error(ValueError("invalid schema")), PermanentExecutionError)


@pytest.mark.asyncio
async def test_active_maintenance_runs_heartbeat_and_lease_renewal(worker, monkeypatch):
    worker.heartbeat_seconds = 0.005
    worker.lease_renew_seconds = 0.015
    calls = []
    async def heartbeat(status="IDLE"):
        calls.append(("heartbeat", status))
    async def renew(_order):
        calls.append(("renew", None))
        if sum(kind == "renew" for kind, _ in calls) >= 3:
            worker.stop()
        return True
    monkeypatch.setattr(worker, "_heartbeat", heartbeat)
    monkeypatch.setattr(worker, "renew_lease", renew)
    lost = asyncio.Event()
    await asyncio.wait_for(worker._active_maintenance_loop("order-1", lost), timeout=0.2)
    assert sum(kind == "heartbeat" for kind, _ in calls) >= 2
    assert sum(kind == "renew" for kind, _ in calls) >= 2


@pytest.mark.asyncio
async def test_lease_loss_is_reported(worker, monkeypatch):
    worker.heartbeat_seconds = 0.001
    worker.lease_renew_seconds = 0.001
    async def heartbeat(_status="IDLE"): pass
    async def renew(_order): return False
    monkeypatch.setattr(worker, "_heartbeat", heartbeat)
    monkeypatch.setattr(worker, "renew_lease", renew)
    lost = asyncio.Event()
    await asyncio.wait_for(worker._active_maintenance_loop("order-1", lost), timeout=0.1)
    assert lost.is_set()


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires PostgreSQL 16 TEST_DATABASE_URL")
@pytest.mark.asyncio
async def test_postgres_departmental_worker_end_to_end(monkeypatch):
    """Real PostgreSQL migrations + tenant repository + orchestration + worker lease lifecycle."""
    import asyncpg
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from app.policy import Principal
    from app.repositories import PostgresWorkforceRepository
    import app.departmental_worker as worker_module
    import app.orchestration as orchestration_module

    monkeypatch.setenv("WORKFORCE_MODEL_PROVIDER", "mock")
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    raw_url = os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    parts = urlsplit(raw_url)
    params = parse_qsl(parts.query)
    sslmode = next((value for key, value in params if key == "sslmode"), None)
    clean_query = urlencode([(key, value) for key, value in params
                             if key not in {"sslmode", "channel_binding"}])
    dsn = urlunsplit((parts.scheme, parts.netloc, parts.path, clean_query, parts.fragment))
    engine_url = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "dept_worker_it_" + uuid.uuid4().hex[:12]
    migration_dir = Path(__file__).resolve().parents[1] / "migrations"
    admin = await asyncpg.connect(dsn, ssl="require" if sslmode == "require" else None)
    engine = None
    try:
        await admin.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        await admin.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}", public')
        for name in (
            "003_durable_workforce_repositories.sql",
            "008_release_gates_and_worker_heartbeats.sql",
            "010_workforce_2_orchestration.sql",
            "014_departmental_worker.sql",
        ):
            await admin.execute((migration_dir / name).read_text(encoding="utf-8"))

        tenant_a, tenant_b = str(uuid.uuid4()), str(uuid.uuid4())
        engine = create_async_engine(
            engine_url, pool_size=8, max_overflow=4,
            connect_args={"server_settings": {"search_path": f"{schema},public"}},
        )
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        async def set_admin_tenant(tenant):
            await admin.execute("SELECT set_config('app.current_tenant_id',$1,false)", tenant)

        async def add_directive(tenant):
            await set_admin_tenant(tenant)
            did = uuid.uuid4()
            await admin.execute(
                "INSERT INTO executive_directives(id,tenant_id,founder_id,command) VALUES($1,$2,'it','Integration objective')",
                did, tenant,
            )
            return did

        async def add_order(tenant, directive, *, department="Growth & Marketing",
                            specialist="market-intel", tool="public_directory_research",
                            status="ASSIGNED", dependencies="[]", attempts=0, max_attempts=3,
                            claimed_by=None, expired=False):
            await set_admin_tenant(tenant)
            oid = uuid.uuid4()
            await admin.execute("""INSERT INTO department_work_orders
              (id,tenant_id,directive_id,department,department_head_id,title,objective,
               dependencies,specialist_role_id,tool_name,tool_input,status,attempt_count,
               max_attempts,claimed_by,locked_by_employee_id,claimed_at,lock_expires_at)
              VALUES($1,$2,$3,$4,'department-head','IT order','objective',$5::jsonb,
                     $6,$7,'{}'::jsonb,$8,$9,$10,$11,$11,
                     CASE WHEN $11::text IS NULL THEN NULL ELSE now()-interval '10 minutes' END,
                     CASE WHEN $12 THEN now()-interval '10 minutes' ELSE now()+interval '5 minutes' END)""",
              oid, tenant, directive, department, dependencies, specialist, tool,
              status, attempts, max_attempts, claimed_by, expired)
            return oid

        async def claim_with(worker_id, tenant):
            principal = Principal(worker_id, tenant, "service", frozenset({"*"}),
                                  actor_type="service", product_ids=frozenset())
            async with factory() as session:
                async with PostgresWorkforceRepository(session, principal) as repo:
                    return await WorkforceOrchestrator(repo, principal).claim_work_order(worker_id)

        # Two independent workers race on one order: PostgreSQL grants one lease.
        directive = await add_directive(tenant_a)
        race_order = await add_order(tenant_a, directive)
        winners = await asyncio.gather(
            claim_with("worker-a", tenant_a),
            claim_with("worker-b", tenant_a),
        )
        assert sum(result is not None and str(result["id"]) == str(race_order) for result in winners) == 1

        # SET LOCAL + tenant predicate/RLS prevents the Tenant A worker seeing Tenant B work.
        directive_b = await add_directive(tenant_b)
        tenant_b_order = await add_order(tenant_b, directive_b)
        principal_a = Principal("tenant-a-worker", tenant_a, "service", frozenset({"*"}),
                                actor_type="service", product_ids=frozenset())
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal_a):
                visible = (await session.execute(text(
                    "SELECT id FROM department_work_orders WHERE status='ASSIGNED'"
                ))).scalars().all()
                assert str(tenant_b_order) not in {str(x) for x in visible}

        # Long-running work proves independent heartbeat sessions and lease renewal
        # advance both persisted timestamps while execute_work_order is still running.
        long_directive = await add_directive(tenant_a)
        long_order = await add_order(tenant_a, long_directive)
        monkeypatch.setenv("RUNGROW_INTERNAL_TENANT_ID", tenant_a)
        worker = worker_module.DepartmentalWorker(tenant_id=tenant_a, heartbeat_seconds=0.01,
            lease_renew_seconds=0.02, lease_seconds=300)
        monkeypatch.setattr(worker, "_factory", lambda: factory)
        monkeypatch.setattr(worker_module, "session_factory", lambda: factory)
        async def slow_execute(self, order):
            await asyncio.sleep(0.035)
            async with factory() as session:
                async with PostgresWorkforceRepository(session, principal_a):
                    heartbeat_one = (await session.execute(text(
                        "SELECT last_heartbeat_at FROM worker_heartbeats WHERE worker_id=:worker"
                    ), {"worker": worker.worker_id})).scalar_one()
                    lease_one = (await session.execute(text(
                        "SELECT lock_expires_at FROM department_work_orders WHERE id=:id"
                    ), {"id": order["id"]})).scalar_one()
            await asyncio.sleep(0.045)
            async with factory() as session:
                async with PostgresWorkforceRepository(session, principal_a):
                    heartbeat_two = (await session.execute(text(
                        "SELECT last_heartbeat_at FROM worker_heartbeats WHERE worker_id=:worker"
                    ), {"worker": worker.worker_id})).scalar_one()
                    lease_two = (await session.execute(text(
                        "SELECT lock_expires_at FROM department_work_orders WHERE id=:id"
                    ), {"id": order["id"]})).scalar_one()
            assert heartbeat_two > heartbeat_one
            assert lease_two > lease_one
            assert heartbeat_two > heartbeat_before["last_heartbeat_at"]
            assert lease_two > order["lock_expires_at"]
            return {"status": "OK"}

        monkeypatch.setattr(orchestration_module.WorkforceOrchestrator, "execute_work_order", slow_execute)
        await worker._heartbeat("STARTING")
        await set_admin_tenant(tenant_a)
        heartbeat_before = await admin.fetchrow(
            "SELECT last_heartbeat_at FROM worker_heartbeats WHERE worker_id=$1", worker.worker_id
        )
        claimed = await worker._claim()
        assert claimed and str(claimed["id"]) == str(long_order)
        initial_lease_expiry = claimed["lock_expires_at"]
        await worker._execute_one(claimed)
        await set_admin_tenant(tenant_a)
        heartbeat = await admin.fetchrow(
            'SELECT last_heartbeat_at,status FROM worker_heartbeats WHERE worker_id=$1', worker.worker_id
        )
        lease = await admin.fetchrow(
            "SELECT lock_expires_at FROM department_work_orders WHERE id=$1", long_order
        )
        assert heartbeat and heartbeat["status"] == "IDLE"
        assert heartbeat_before and heartbeat["last_heartbeat_at"] > heartbeat_before["last_heartbeat_at"]
        assert lease and lease["lock_expires_at"] > initial_lease_expiry

        # Atomic stale-lease recovery: attempts below max requeue; at max fail
        # without incrementing beyond max_attempts.
        stale_directive = await add_directive(tenant_a)
        below = await add_order(tenant_a, stale_directive, status="IN_PROGRESS",
                                attempts=1, max_attempts=3, claimed_by="dead-worker", expired=True)
        at_max = await add_order(tenant_a, stale_directive, department="Finance & Administration",
                                 specialist="finance", tool="campaign_budget_estimate",
                                 status="IN_PROGRESS", attempts=3, max_attempts=3,
                                 claimed_by="dead-worker", expired=True)
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal_a) as repo:
                recovered = await WorkforceOrchestrator(repo, principal_a).recover_stale_leases()
                assert recovered == 2
        await set_admin_tenant(tenant_a)
        below_row = await admin.fetchrow("SELECT status,attempt_count FROM department_work_orders WHERE id=$1", below)
        max_row = await admin.fetchrow("SELECT status,attempt_count FROM department_work_orders WHERE id=$1", at_max)
        assert (below_row["status"], below_row["attempt_count"]) == ("QUEUED", 2)
        assert (max_row["status"], max_row["attempt_count"]) == ("FAILED", 3)

        # Approval dependency gate remains enforced by the real claim query.
        dep_directive = await add_directive(tenant_a)
        dependent = await add_order(tenant_a, dep_directive, department="Sales & Revenue",
                                    specialist="lead-research", tool="icp_lead_scoring",
                                    dependencies='["Growth & Marketing"]')
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal_a) as repo:
                gated = await WorkforceOrchestrator(repo, principal_a).claim_work_order("dependency-worker")
                # Any other eligible work has a retry delay or is already claimed; dependent
                # itself cannot be claimed until its prerequisite is APPROVED_BY_HEAD.
                assert gated is None
        await set_admin_tenant(tenant_a)
        await admin.execute("""INSERT INTO department_work_orders
          (tenant_id,directive_id,department,department_head_id,title,objective,
           specialist_role_id,tool_name,tool_input,status)
          VALUES($1,$2,'Growth & Marketing','head','Prerequisite','objective',
                 'market-intel','public_directory_research','{}'::jsonb,'APPROVED_BY_HEAD')""",
          tenant_a, dep_directive)
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal_a) as repo:
                released = await WorkforceOrchestrator(repo, principal_a).claim_work_order("dependency-worker")
                assert released and str(released["id"]) == str(dependent)

        # Transient failure rolls back execution and schedules a bounded retry in a
        # separate transaction; policy denial and missing role fail terminally.
        class TimeoutTools:
            directory_fetcher = None
            def execute(self, **_kwargs): raise TimeoutError("upstream timeout")

        transient_directive = await add_directive(tenant_a)
        transient_id = await add_order(tenant_a, transient_directive, department="Sales & Revenue",
            specialist="lead-research", tool="icp_lead_scoring", status="IN_PROGRESS",
            claimed_by="transient-worker")
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal_a):
                order = dict((await session.execute(text(
                    "SELECT * FROM department_work_orders WHERE id=:id"
                ), {"id": transient_id})).mappings().one())
        with pytest.raises(TransientExecutionError):
            async with factory() as session:
                async with PostgresWorkforceRepository(session, principal_a) as repo:
                    await WorkforceOrchestrator(repo, principal_a, TimeoutTools()).execute_work_order(order)
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal_a) as repo:
                await WorkforceOrchestrator(repo, principal_a).schedule_retry(
                    order, "transient-worker", TimeoutError("upstream timeout"), 3
                )
        await set_admin_tenant(tenant_a)
        transient_row = await admin.fetchrow(
            "SELECT status,attempt_count FROM department_work_orders WHERE id=$1", transient_id
        )
        assert (transient_row["status"], transient_row["attempt_count"]) == ("QUEUED", 1)

        class DeniedTools:
            directory_fetcher = None
            def execute(self, **_kwargs): raise PermissionError("policy denied")

        denied_directive = await add_directive(tenant_a)
        denied_id = await add_order(tenant_a, denied_directive, department="Sales & Revenue",
            specialist="lead-research", tool="icp_lead_scoring", status="IN_PROGRESS",
            claimed_by="denied-worker")
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal_a) as repo:
                denied_order = dict((await session.execute(text(
                    "SELECT * FROM department_work_orders WHERE id=:id"
                ), {"id": denied_id})).mappings().one())
                result = await WorkforceOrchestrator(repo, principal_a, DeniedTools()).execute_work_order(denied_order)
                assert result["status"] == "REJECTED"
        await set_admin_tenant(tenant_a)
        denied_row = await admin.fetchrow(
            "SELECT status,attempt_count,last_error FROM department_work_orders WHERE id=$1", denied_id
        )
        assert denied_row["status"] == "REJECTED" and denied_row["attempt_count"] == 0
        assert "policy denied" in denied_row["last_error"]

        missing_directive = await add_directive(tenant_a)
        missing_id = await add_order(tenant_a, missing_directive, specialist="not-registered",
            status="IN_PROGRESS", claimed_by="missing-worker")
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal_a) as repo:
                missing_order = dict((await session.execute(text(
                    "SELECT * FROM department_work_orders WHERE id=:id"
                ), {"id": missing_id})).mappings().one())
                result = await WorkforceOrchestrator(repo, principal_a).execute_work_order(missing_order)
                assert result["status"] == "REJECTED"
        await set_admin_tenant(tenant_a)
        missing_row = await admin.fetchrow(
            "SELECT status,attempt_count,last_error FROM department_work_orders WHERE id=$1", missing_id
        )
        assert missing_row["status"] == "REJECTED" and missing_row["attempt_count"] == 0
        assert "Unregistered specialist role" in missing_row["last_error"]
    finally:
        if engine is not None:
            await engine.dispose()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
