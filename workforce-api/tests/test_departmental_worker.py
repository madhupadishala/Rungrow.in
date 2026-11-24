from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest

from app.departmental_worker import DepartmentalWorker, LeaseLostError


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("RUNGROW_INTERNAL_TENANT_ID", "11111111-1111-1111-1111-111111111111")
    return DepartmentalWorker(tenant_id="11111111-1111-1111-1111-111111111111")


def test_worker_identity_is_unique_and_tenant_scoped(worker):
    other = DepartmentalWorker(tenant_id=worker.tenant_id)
    assert worker.worker_id != other.worker_id
    principal = worker._principal()
    assert principal.tenant_id == worker.tenant_id
    assert principal.actor_type == "service"


def test_stop_is_idempotent(worker):
    worker.stop()
    worker.stop()
    assert worker._stop.is_set()


@pytest.mark.asyncio
async def test_renewal_returns_false_when_lease_lost(worker, monkeypatch):
    class Result:
        rowcount = 0
    class Session:
        async def execute(self, *_args, **_kwargs):
            return Result()
    class RepoContext:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
    class SessionContext:
        async def __aenter__(self): return session
        async def __aexit__(self, *_args): return False
    session = Session()
    class Factory:
        def __call__(self): return SessionContext()
    monkeypatch.setattr(worker, "_factory", lambda: Factory())
    monkeypatch.setattr("app.departmental_worker.PostgresWorkforceRepository",
                        lambda *_args: RepoContext())
    assert await worker.renew_lease(str(uuid.uuid4())) is False


@pytest.mark.asyncio
async def test_run_exits_cleanly_when_stopped(worker, monkeypatch):
    calls = []
    async def heartbeat(status="RUNNING"):
        calls.append(status)
    async def claim():
        worker.stop()
        return None
    monkeypatch.setattr(worker, "_heartbeat", heartbeat)
    monkeypatch.setattr(worker, "_claim", claim)
    await worker.run()
    assert calls[0] == "RUNNING"
    assert calls[-1] == "STOPPED"


@pytest.mark.asyncio
async def test_polling_backoff_is_bounded(worker, monkeypatch):
    worker.poll_min = 0.001
    worker.poll_max = 0.004
    attempts = 0
    async def heartbeat(_status="RUNNING"): pass
    async def claim():
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            worker.stop()
        return None
    monkeypatch.setattr(worker, "_heartbeat", heartbeat)
    monkeypatch.setattr(worker, "_claim", claim)
    await worker.run()
    assert attempts == 3


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"),
                    reason="PostgreSQL integration requires TEST_DATABASE_URL")
@pytest.mark.asyncio
async def test_postgres_skip_locked_and_set_local_tenant_scope():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    url = os.environ["TEST_DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, pool_pre_ping=True)
    table = "worker_it_" + uuid.uuid4().hex[:12]
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"""
                CREATE TABLE {table} (
                    id integer PRIMARY KEY,
                    tenant_id uuid NOT NULL,
                    status text NOT NULL,
                    claimed_by text
                )
            """))
            await conn.execute(text(f"INSERT INTO {table} VALUES (1,:a,'QUEUED',NULL),(2,:b,'QUEUED',NULL)"),
                               {"a": tenant_a, "b": tenant_b})
        async with engine.connect() as c1, engine.connect() as c2:
            tx1 = await c1.begin()
            tx2 = await c2.begin()
            await c1.execute(text("SELECT set_config('app.current_tenant_id',:tenant,true)"),
                             {"tenant": tenant_a})
            await c2.execute(text("SELECT set_config('app.current_tenant_id',:tenant,true)"),
                             {"tenant": tenant_a})
            first = await c1.execute(text(f"SELECT id FROM {table} WHERE tenant_id=:tenant AND status='QUEUED' FOR UPDATE SKIP LOCKED"),
                                     {"tenant": tenant_a})
            second = await c2.execute(text(f"SELECT id FROM {table} WHERE tenant_id=:tenant AND status='QUEUED' FOR UPDATE SKIP LOCKED"),
                                      {"tenant": tenant_a})
            assert first.scalar_one() == 1
            assert second.scalar_one_or_none() is None
            await tx1.rollback()
            await tx2.rollback()
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text("SELECT set_config('app.current_tenant_id',:tenant,true)"),
                                   {"tenant": tenant_a})
                rows = (await conn.execute(text(f"SELECT id FROM {table} WHERE tenant_id=:tenant"),
                                           {"tenant": tenant_a})).scalars().all()
                assert rows == [1]
                other = (await conn.execute(text(f"SELECT id FROM {table} WHERE tenant_id=:tenant"),
                                            {"tenant": tenant_b})).scalars().all()
                assert other == [2]
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        await engine.dispose()
