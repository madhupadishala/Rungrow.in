"""Lease-based durable task orchestration. Workers must make side effects idempotent."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
import asyncio, random

class DurableTaskOrchestrator:
    def __init__(self, repository, worker_id: str, lease_seconds: int=60, max_attempts: int=5):
        self.repository=repository; self.worker_id=worker_id
        self.lease_seconds=lease_seconds; self.max_attempts=max_attempts

    async def claim(self, product_id=None):
        return await self.repository.claim_task(self.worker_id,self.lease_seconds,product_id)

    async def dispatch_once(self, handler: Callable[[dict],Awaitable[Any]], product_id=None):
        task=await self.claim(product_id)
        if not task: return None
        try:
            result=await handler(task)
            await self.repository.finish_task(task["id"],self.worker_id,result)
            return {"task_id":task["id"],"status":"COMPLETED","result":result}
        except Exception as exc:
            delay=min(300,2 ** max(1,int(task.get("attempt_count",1))))
            await self.repository.fail_task(task["id"],self.worker_id,str(exc),delay,self.max_attempts)
            raise

    async def run(self, handler, *, poll_seconds: float=1, stop: asyncio.Event | None=None):
        while stop is None or not stop.is_set():
            try:
                result=await self.dispatch_once(handler)
            except Exception:
                result=None
            if result is None: await asyncio.sleep(poll_seconds)

