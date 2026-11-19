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
}
_events=defaultdict(deque)

def reset_rate_limits():
    _events.clear()

def _policy(path: str):
    for prefix,policy in POLICIES.items():
        if path.startswith(prefix): return policy
    return None

def _client_ip(request: Request) -> str:
    # Never trust arbitrary X-Forwarded-For unless a trusted proxy has normalized it.
    return request.client.host if request.client else "unknown"

async def _allow_postgres(scope_key: str, limit: int, window: int):
    factory=session_factory()
    async with factory() as session:
        async with session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),{"scope":scope_key})
            await session.execute(text("DELETE FROM rate_limit_events WHERE scope_key=:scope AND occurred_at < now() - make_interval(secs => :window)"),
                                  {"scope":scope_key,"window":window})
            count=(await session.execute(text("SELECT count(*) FROM rate_limit_events WHERE scope_key=:scope AND occurred_at >= now() - make_interval(secs => :window)"),
                                         {"scope":scope_key,"window":window})).scalar_one()
            if count>=limit:
                oldest=(await session.execute(text("SELECT min(occurred_at) FROM rate_limit_events WHERE scope_key=:scope"),
                                              {"scope":scope_key})).scalar_one()
                remaining=max(1,window-int((time.time()-oldest.timestamp())))
                return False,remaining
            await session.execute(text("INSERT INTO rate_limit_events(scope_key) VALUES (:scope)"),{"scope":scope_key})
            return True,0

async def _allow_local(scope_key: str, limit: int, window: int):
    now=time.monotonic(); q=_events[scope_key]
    while q and q[0] <= now-window: q.popleft()
    if len(q)>=limit: return False,max(1,int(window-(now-q[0])))
    q.append(now); return True,0

class RateLimitMiddleware:
    def __init__(self, app):
        self.app=app
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
        # Bearer token hash partitions authenticated users without retaining credentials.
        auth=headers.get("authorization","")
        actor=hashlib.sha256(auth.encode()).hexdigest()[:16] if auth else "anonymous"
        client=scope.get("client")
        ip=client[0] if client else "unknown"
        scope_key=hashlib.sha256(f"{path.split('?')[0]}|{tenant}|{actor}|{ip}".encode()).hexdigest()
        try:
            allowed,retry=await _allow_postgres(scope_key,limit,window) if settings.database_url else await _allow_local(scope_key,limit,window)
        except Exception:
            if settings.app_env=="production":
                # Abuse protection is a security dependency: fail closed if the shared limiter is unavailable.
                response=b'{"detail":"Rate limiting dependency unavailable"}'
                await send({"type":"http.response.start","status":503,"headers":[(b"content-type",b"application/json")]})
                await send({"type":"http.response.body","body":response}); return
            allowed,retry=await _allow_local(scope_key,limit,window)
        if not allowed:
            response=b'{"detail":"Rate limit exceeded"}'
            await send({"type":"http.response.start","status":429,"headers":[(b"content-type",b"application/json"),(b"retry-after",str(retry).encode())]})
            await send({"type":"http.response.body","body":response}); return
        await self.app(scope,receive,send)
