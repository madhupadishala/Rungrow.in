"""Tenant-scoped async PostgreSQL repositories for RunGrow Workforce.

All methods execute inside a transaction that installs verified Principal scope
with SET LOCAL. No repository method accepts tenant_id from request data.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .policy import Principal


@lru_cache(maxsize=1)
def session_factory():
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for Workforce API runtime")
    parts = urlsplit(settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1))
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"sslmode", "channel_binding"}]
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    connect_args = {"ssl": "require"} if "sslmode=" in settings.database_url else {}
    engine = create_async_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


from .reporting_repository import ReportingRepositoryMixin
from .release_repository import ReleaseRepositoryMixin

class PostgresWorkforceRepository(ReleaseRepositoryMixin, ReportingRepositoryMixin):
    def __init__(self, session: AsyncSession, principal: Principal):
        self.session = session
        self.principal = principal

    async def __aenter__(self):
        await self.session.begin()
        await self.session.execute(
            text("SELECT set_config('app.current_tenant_id', :tenant, true)"),
            {"tenant": self.principal.tenant_id},
        )
        await self.session.execute(
            text("SELECT set_config('app.product_ids', :products, true)"),
            {"products": ",".join(sorted(self.principal.product_ids))},
        )
        await self.session.execute(
            text("SELECT set_config('app.current_principal_id', :principal, true)"),
            {"principal": self.principal.user_id},
        )
        await self.session.execute(
            text("SELECT set_config('app.is_company_admin', :admin, true)"),
            {"admin": "true" if "*" in self.principal.permissions else "false"},
        )
        await self.session.execute(
            text("SELECT set_config('app.can_decide_approvals', :allowed, true)"),
            {"allowed": "true" if "*" in self.principal.permissions or "approval.decide" in self.principal.permissions else "false"},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            await self.session.rollback()
        else:
            await self.session.commit()

    async def _rows(self, sql: str, params: dict[str, Any] | None = None):
        result = await self.session.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings().all()]

    async def _one(self, sql: str, params: dict[str, Any] | None = None):
        result = await self.session.execute(text(sql), params or {})
        row = result.mappings().first()
        return dict(row) if row else None

    @staticmethod
    def _product(product_id: str | None):
        if product_id is not None and (not isinstance(product_id, str) or not product_id.strip()):
            raise ValueError("Invalid product scope")

    async def create_task(self, *, product_id: str | None, department: str, assignee_role_id: str,
                          title: str, description: str, priority: str, idempotency_key: str,
                          is_sandbox: bool=False, max_attempts: int=5):
        self._product(product_id)
        item = await self._one("""
          INSERT INTO wf_tasks
            (id, tenant_id, product_id, department, assignee_role_id, title, description,
             priority, status, created_by, idempotency_key, created_at, is_sandbox, max_attempts)
          VALUES (:id, :tenant, :product, :department, :assignee, :title, :description,
                  :priority, 'QUEUED', :actor, :idem, now(), :sandbox, :max_attempts)
          ON
