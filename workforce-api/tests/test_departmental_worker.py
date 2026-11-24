from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.departmental_worker import DepartmentalWorker, LeaseLostError
from app.orchestration import WorkforceOrchestrator, PermanentExecutionError, TransientExecutionError, classify_execution_error


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
    assert isinstance(classify_execution_error(ValueError("bad schema")), PermanentExecutionError)


@pytest.mark.asyncio
async def test_active_loop_heartbeats_and_renews_concurrently(worker, monkeypatch):
    worker.heartbeat_seconds = 0.01
    worker.lease_renew_seconds = 0.025
    calls = []
    async def heartbeat(status="IDLE"):
        calls.append(("heartbeat", status))
    async def renew(_order):
        calls.append(("renew", None))
        if sum(1 for kind, _ in calls if kind == "renew") >= 3:
            worker.stop()
        return True
    monkeypatch.setattr(worker, "_heartbeat", heartbeat)
    monkeypatch.setattr(worker, "renew_lease", renew)
    lost = asyncio.Event()
    await asyncio.wait_for(worker._active_maintenance_loop("order-1", lost), timeout=0.2)
    assert sum(1 for kind, _ in calls if kind == "heartbeat") >= 2
    assert sum(1 for kind, _ in calls if kind == "renew") >= 2


@pytest.mark.asyncio
async def test_lease_loss_signals_immediately(worker, monkeypatch):
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
async def test_postgres_departmental_worker_production_path():
    """Apply the real departmental schema migrations and exercise real repository/orchestrator claims."""
    import asyncpg
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from app.repositories import PostgresWorkforceRepository
    from app.policy import Principal

    dsn = os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    schema = "worker_it_" + uuid.uuid4().hex[:12]
    root = Path(__file__).resolve().parents[1] / "migrations"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        # Canonical migrations: 008 defines the existing heartbeat table, 010
        # defines department_work_orders, and 014 extends both idempotently.
        for filename in ("008_release_gates_and_worker_heartbeats.sql",
                         "010_workforce_2_orchestration.sql",
                         "014_departmental_worker.sql"):
            await conn.execute((root / filename).read_text(encoding="utf-8"))
        tenant_a, tenant_b = str(uuid.uuid4()), str(uuid.uuid4())
        directive_a, directive_b = str(uuid.uuid4()), str(uuid.uuid4())
        await conn.execute("""INSERT INTO executive_directives(id,tenant_id,founder_id,command)
          VALUES($1,$2,'founder','Run integration test objective'),($3,$4,'founder','Other tenant objective')""",
          uuid.UUID(directive_a), tenant_a, uuid.UUID(directive_b), tenant_b)
        await conn.execute("""INSERT INTO department_work_orders
          (id,tenant_id,directive_id,department,department_head_id,title,objective,
           specialist_role_id,tool_name,tool_input,status)
          VALUES($1,$2,$3,'Growth & Marketing','growth-director','Claim A','objective',
                 'market-intel','public_directory_research','{}','ASSIGNED'),
                ($4,$5,$6,'Growth & Marketing','growth-director','Claim B','objective',
                 'market-intel','public_directory_research','{}','ASSIGNED')""",
          uuid.uuid4(), tenant_a, uuid.UUID(directive_a),
          uuid.uuid4(), tenant_b, uuid.UUID(directive_b))
        engine = create_async_engine(dsn, connect_args={"server_settings":{"search_path":schema}})
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        principal = Principal("integration-worker", tenant_a, "service", frozenset({"*"}),
                              actor_type="service", product_ids=frozenset())
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal) as repo:
                claimed = await WorkforceOrchestrator(repo, principal).claim_work_order("integration-worker")
                assert claimed and claimed["tenant_id"] == tenant_a
                assert claimed["status"] == "IN_PROGRESS"
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal):
                visible = (await session.execute(text("SELECT count(*) FROM department_work_orders"))).scalar_one()
                assert visible == 1
        await engine.dispose()
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
