"""Tenant-scoped consumer for department_work_orders (not wf_tasks)."""
from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text

from .config import settings
from .policy import Principal
from .repositories import PostgresWorkforceRepository, session_factory
from .orchestration import WorkforceOrchestrator


class LeaseLostError(RuntimeError):
    """Raised when a worker no longer owns a live work-order lease."""


class DepartmentalWorker:
    def __init__(self, *, tenant_id: str | None = None, poll_min: float = 1.0,
                 poll_max: float = 20.0, lease_seconds: int = 300,
                 heartbeat_seconds: int = 15, max_attempts: int = 3):
        self.tenant_id = (tenant_id or os.getenv("RUNGROW_INTERNAL_TENANT_ID", "")).strip()
        if not self.tenant_id:
            raise RuntimeError("RUNGROW_INTERNAL_TENANT_ID is required for departmental worker")
        self.worker_id = f"dept-worker-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        self.poll_min, self.poll_max = poll_min, poll_max
        self.lease_seconds, self.heartbeat_seconds = lease_seconds, heartbeat_seconds
        self.max_attempts = max_attempts
        self._stop = asyncio.Event()
        self._current_order_id: str | None = None
        self._factory = session_factory

    def stop(self) -> None:
        self._stop.set()

    def _principal(self) -> Principal:
        # Wildcard permissions are scoped to exactly one internal tenant.
        return Principal(self.worker_id, self.tenant_id, "service",
                         frozenset({"*"}), actor_type="service",
                         product_ids=frozenset())

    async def _repo(self):
        factory = self._factory()
        session = factory()
        return factory, session

    async def _heartbeat(self, status: str = "RUNNING") -> None:
        factory, session = await self._repo()
        async with session:
            async with PostgresWorkforceRepository(session, self._principal()) as repo:
                await session.execute(text("""
                    INSERT INTO worker_heartbeats
                      (worker_id, tenant_id, started_at, last_heartbeat_at, status, current_work_order_id)
                    VALUES (:worker, :tenant, now(), now(), :status, :current)
                    ON CONFLICT (worker_id) DO UPDATE SET
                      last_heartbeat_at=now(), status=EXCLUDED.status,
                      current_work_order_id=EXCLUDED.current_work_order_id
                    WHERE worker_heartbeats.tenant_id=EXCLUDED.tenant_id
                """), {"worker": self.worker_id, "tenant": self.tenant_id,
                       "status": status, "current": self._current_order_id})

    async def _claim(self) -> dict | None:
        factory, session = await self._repo()
        async with session:
            async with PostgresWorkforceRepository(session, self._principal()) as repo:
                orchestrator = WorkforceOrchestrator(repo, self._principal())
                return await orchestrator.claim_work_order(self.worker_id)

    async def renew_lease(self, work_order_id: str) -> bool:
        factory, session = await self._repo()
        async with session:
            async with PostgresWorkforceRepository(session, self._principal()):
                result = await session.execute(text("""
                    UPDATE department_work_orders
                    SET lock_expires_at=now() + (:lease_seconds * interval '1 second'),
                        updated_at=now()
                    WHERE id=:id AND tenant_id=:tenant AND claimed_by=:worker
                      AND status='IN_PROGRESS' AND lock_expires_at > now()
                """), {"id": work_order_id, "tenant": self.tenant_id,
                       "worker": self.worker_id, "lease_seconds": self.lease_seconds})
                return result.rowcount == 1

    async def _execute_one(self, order: dict) -> None:
        self._current_order_id = str(order["id"])
        stop_renewal = asyncio.Event()

        async def renew_loop():
            while not stop_renewal.is_set():
                try:
                    await asyncio.wait_for(stop_renewal.wait(), timeout=max(5, self.lease_seconds / 3))
                    return
                except asyncio.TimeoutError:
                    if not await self.renew_lease(str(order["id"])):
                        raise LeaseLostError(f"Lease lost for work order {order['id']}")

        renew_task = asyncio.create_task(renew_loop())
        execution_task = None
        try:
            factory, session = await self._repo()
            async with session:
                async with PostgresWorkforceRepository(session, self._principal()) as repo:
                    orchestrator = WorkforceOrchestrator(repo, self._principal())
                    execution_task = asyncio.create_task(orchestrator.execute_work_order(order))
                    done, _ = await asyncio.wait(
                        {execution_task, renew_task}, return_when=asyncio.FIRST_COMPLETED)
                    if renew_task in done:
                        exc = renew_task.exception()
                        if exc:
                            execution_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await execution_task
                            raise exc
                    result = await execution_task
                    return result
        except asyncio.CancelledError:
            if execution_task and not execution_task.done():
                execution_task.cancel()
            raise
        finally:
            stop_renewal.set()
            renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await renew_task
            self._current_order_id = None

    async def run(self) -> None:
        delay = self.poll_min
        await self._heartbeat("RUNNING")
        try:
            while not self._stop.is_set():
                await self._heartbeat("RUNNING")
                try:
                    order = await self._claim()
                    if order is None:
                        try:
                            await asyncio.wait_for(self._stop.wait(), timeout=delay)
                        except asyncio.TimeoutError:
                            pass
                        delay = min(self.poll_max, max(self.poll_min, delay * 1.7))
                        continue
                    delay = self.poll_min
                    await self._execute_one(order)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The orchestrator owns business-state transitions. A worker-level
                    # failure is logged by the service runtime and the lease expires safely.
                    await asyncio.sleep(delay)
                    delay = min(self.poll_max, max(self.poll_min, delay * 2))
        finally:
            with suppress(Exception):
                await self._heartbeat("STOPPED")

