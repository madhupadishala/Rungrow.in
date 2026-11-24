"""Tenant-scoped consumer for department_work_orders (not wf_tasks)."""
from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from uuid import UUID, uuid4

from sqlalchemy import text

from .policy import Principal
from .repositories import PostgresWorkforceRepository, session_factory
from .orchestration import WorkforceOrchestrator, TransientExecutionError


class LeaseLostError(RuntimeError):
    """Raised when a worker no longer owns a live work-order lease."""


class DepartmentalWorker:
    def __init__(self, *, tenant_id: str | None = None, poll_min: float = 1.0,
                 poll_max: float = 20.0, lease_seconds: int = 300,
                 heartbeat_seconds: int = 15, lease_renew_seconds: int = 60,
                 max_attempts: int = 3):
        self.tenant_id = (tenant_id or os.getenv("RUNGROW_INTERNAL_TENANT_ID", "")).strip()
        if not self.tenant_id:
            raise RuntimeError("RUNGROW_INTERNAL_TENANT_ID is required for departmental worker")
        self.worker_id = f"dept-worker-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        self.poll_min, self.poll_max = poll_min, poll_max
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_renew_seconds = lease_renew_seconds
        self.max_attempts = max_attempts
        self._stop = asyncio.Event()
        self._current_order_id: str | None = None
        self._factory = session_factory

    def stop(self) -> None:
        self._stop.set()

    def _principal(self) -> Principal:
        return Principal(self.worker_id, self.tenant_id, "service",
                         frozenset({"*"}), actor_type="service",
                         product_ids=frozenset())

    async def _repo(self):
        factory = self._factory()
        return factory, factory()

    async def _heartbeat(self, status: str = "IDLE") -> None:
        # The established 008 schema uses STARTING/IDLE/BUSY/STOPPING/ERROR.
        state = {"RUNNING": "IDLE", "STOPPED": "STOPPING"}.get(status, status)
        factory, session = await self._repo()
        async with session:
            async with PostgresWorkforceRepository(session, self._principal()):
                await session.execute(text("""
                    INSERT INTO worker_heartbeats
                      (worker_id, tenant_id, started_at, last_heartbeat_at, status, current_work_order_id)
                    VALUES (:worker, :tenant, now(), now(), :status, :current)
                    ON CONFLICT (worker_id) DO UPDATE SET
                      last_heartbeat_at=now(), status=EXCLUDED.status,
                      current_work_order_id=EXCLUDED.current_work_order_id
                    WHERE worker_heartbeats.tenant_id=EXCLUDED.tenant_id
                """), {"worker": self.worker_id, "tenant": self.tenant_id,
                       "status": state, "current": UUID(self._current_order_id) if self._current_order_id else None})

    async def _claim(self) -> dict | None:
        factory, session = await self._repo()
        async with session:
            async with PostgresWorkforceRepository(session, self._principal()) as repo:
                orchestrator = WorkforceOrchestrator(repo, self._principal())
                await orchestrator.recover_stale_leases()
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
                """), {"id": UUID(work_order_id), "tenant": self.tenant_id,
                       "worker": self.worker_id, "lease_seconds": self.lease_seconds})
                return result.rowcount == 1

    async def _active_maintenance_loop(self, work_order_id: str,
                                       lease_lost: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        next_renewal = loop.time() + self.lease_renew_seconds
        while not self._stop.is_set() and not lease_lost.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(0.001, min(self.heartbeat_seconds, next_renewal - loop.time())),
                )
                return
            except asyncio.TimeoutError:
                pass

            # Each heartbeat and renewal uses its own short-lived session. Neither
            # operation can contend with or share the execution transaction.
            try:
                await self._heartbeat("BUSY")
                if loop.time() >= next_renewal:
                    if not await self.renew_lease(work_order_id):
                        lease_lost.set()
                        return
                    next_renewal = loop.time() + self.lease_renew_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not silently continue execution when the lease cannot be proven.
                # A failed renewal tick is treated as loss of ownership.
                lease_lost.set()
                return

    async def _execute_one(self, order: dict) -> None:
        work_order_id = str(order["id"])
        self._current_order_id = work_order_id
        lease_lost = asyncio.Event()
        execution_task = None
        maintenance_task = asyncio.create_task(
            self._active_maintenance_loop(work_order_id, lease_lost)
        )
        try:
            factory, session = await self._repo()
            async with session:
                async with PostgresWorkforceRepository(session, self._principal()) as repo:
                    orchestrator = WorkforceOrchestrator(repo, self._principal())
                    execution_task = asyncio.create_task(orchestrator.execute_work_order(order))
                    lost_task = asyncio.create_task(lease_lost.wait())
                    done, _ = await asyncio.wait(
                        {execution_task, lost_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if lost_task in done and lease_lost.is_set():
                        execution_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await execution_task
                        raise LeaseLostError(f"Lease lost for work order {work_order_id}")
                    lost_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await lost_task
                    await execution_task
        except asyncio.CancelledError:
            if execution_task and not execution_task.done():
                execution_task.cancel()
                with suppress(asyncio.CancelledError):
                    await execution_task
            raise
        except TransientExecutionError as exc:
            # The execution transaction has rolled back before retry scheduling.
            retry_factory, retry_session = await self._repo()
            async with retry_session:
                async with PostgresWorkforceRepository(retry_session, self._principal()) as retry_repo:
                    await WorkforceOrchestrator(retry_repo, self._principal()).schedule_retry(
                        order, self.worker_id, exc, self.max_attempts
                    )
        finally:
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance_task
            self._current_order_id = None
            with suppress(Exception):
                await self._heartbeat("IDLE")

    async def run(self) -> None:
        delay = self.poll_min
        await self._heartbeat("STARTING")
        try:
            while not self._stop.is_set():
                try:
                    order = await self._claim()
                    if order is None:
                        await self._heartbeat("IDLE")
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
                    await self._heartbeat("ERROR")
                    await asyncio.sleep(delay)
                    delay = min(self.poll_max, max(self.poll_min, delay * 2))
        finally:
            with suppress(Exception):
                await self._heartbeat("STOPPING")
