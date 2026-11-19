"""Tenant/IP-aware sliding-window limiter backed by PostgreSQL in production."""
from __future__ import annotations
import hashlib, time
from collections import defaultdict, deque
from fastapi import Request
from sqlalchemy import text
from .config import settings
from .repositories import session_factory

POLICIES={
 "/v1/auth":(8,60),
 "/v1/adapters/":(120,60),
 "/v1/workforce/orchestrator/dispatch":(30,60),
 "/v1/workforce/orchestrator/step":(30,60),
 "/v1/ceo/directives":(20,60),
 "/v1/commercial/prospects/discover":(10,60),
 "/v1/":(300,60),
}
_events=defaultdict(deque)

def reset_rate_limits():
    _events.clear()

def _policy(path: str):
    for prefix,policy in POLICIES.items():
        if path.startswith(prefix): return policy
    return None

async def _allow_postgres(scope_keys: list[str], limit: int, window: int):
    factory=session_factory()
    async with factory() as session:
        async with session.begin():
            for key in sorted(scope_keys):
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),{"scope":key})
            retry=0
            for key in scope_keys:
                await session.execute(text("DELETE FROM rate_limit_events WHERE scope_key=:scope AND occurred_at < now() - make_interval(secs => :window)"),
                                      {"scope":key,"window":window})
                count=(await session.execute(text("SELECT count(*) FROM rate_limit_events WHERE scope_key=:scope AND occurred_at >= now() - make_interval(secs => :window)"),
                                             {"scope":key,"window":window})).scalar_one()
                if count>=limit:
                    oldest=(await session.execute(text("SELECT min(occurred_at) FROM rate_limit_events WHERE scope_key=:scope"),
                                                  {"scope":key})).scalar_one()
                    retry=max(retry,max(1,window-int((time.time()-oldest.timestamp()))))
            if retry:
                return False,retry
            for key in scope_keys:
                await session.execute(text("INSERT INTO rate_limit_events(scope_key) VALUES (:scope)"),{"scope":key})
            return True,0

async def _allow_local(scope_keys: list[str], limit: int, window: int):
    now=time.monotonic()
    retry=0
    for key in scope_keys:
        q=_events[key]
        while q and q[0] <= now-window: q.popleft()
        if len(q)>=limit:
            retry=max(retry,max(1,int(window-(now-q[0]))))
    if retry: return False,retry
    for key in scope_keys: _events[key].append(now)
    return True,0

class RateLimitMiddleware:
    def __init__(self, app, use_local=False):
        self.app=app
        self.use_local=use_local
    async def __call__(self, scope, receive, send):
        if scope["type"]!="http":
            await self.app(scope,receive,send); return
        path=scope.get("path","")
        policy=_policy(path)
        if not policy:
            await self.app(scope,receive,send); return
        limit,window=policy
        headers={k.decode("latin1").lower():v.decode("latin1") for k,v in scope.get("headers",[])}
        tenant=headers.get("x-rungrow-tenant-id") or headers.get("x-rungrow-external-tenant-id") or "anonymous"
        client=scope.get("client")
        ip=client[0] if client else "unknown"
        normalized_path=path.split("?")[0]
        ip_key=hashlib.sha256(f"{normalized_path}|ip|{ip}".encode()).hexdigest()
        tenant_key=hashlib.sha256(f"{normalized_path}|tenant|{tenant}|{ip}".encode()).hexdigest()
        scope_keys=[ip_key,tenant_key]
        try:
            allowed,retry=await _allow_postgres(scope_keys,limit,window) if settings.database_url and not self.use_local else await _allow_local(scope_keys,limit,window)
        except Exception:
            if settings.app_env=="production":
                response=b'{"detail":"Rate limiting dependency unavailable"}'
                await send({"type":"http.response.start","status":503,"headers":[(b"content-type",b"application/json")]})
                await send({"type":"http.response.body","body":response}); return
            allowed,retry=await _allow_local(scope_keys,limit,window)
        if not allowed:
            response=b'{"detail":"Rate limit exceeded"}'
            await send({"type":"http.response.start","status":429,"headers":[(b"content-type",b"application/json"),(b"retry-after",str(retry).encode())]})
            await send({"type":"http.response.body","body":response}); return
        await self.app(scope,receive,send)
