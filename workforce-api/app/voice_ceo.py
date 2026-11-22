"""Tenant-bound LiveKit room sessions and durable conversational turns."""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from sqlalchemy import text

AI_IDENTITY_DISCLOSURE = "I am RunGrow's AI Digital CEO, not a human. I can brief you, coordinate work, and prepare governed actions. I will never execute an external action without your explicit approval."
LIVEKIT_URL_ENV = "LIVEKIT_URL"
LIVEKIT_API_KEY_ENV = "LIVEKIT_API_KEY"
LIVEKIT_API_SECRET_ENV = "LIVEKIT_API_SECRET"


class LiveKitNotConfigured(RuntimeError):
    def __init__(self, session_id: str):
        super().__init__("LiveKit room service is not configured; no live room or participant token was created.")
        self.session_id = session_id


class VoiceSessionForbidden(PermissionError):
    pass


class VoiceCEOSessionManager:
    """Owns authenticated room minting and tenant-isolated turn persistence.

    The room token is a standard LiveKit HS256 access token. No SDK/network call
    is needed to mint it, which keeps local CI deterministic and offline-safe.
    """

    def __init__(self, *, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _require(principal, permission: str):
        if permission not in principal.permissions and "*" not in principal.permissions:
            raise VoiceSessionForbidden(f"Permission required: {permission}")

    @staticmethod
    def _room(tenant_id: str, session_id: str) -> str:
        tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]
        return f"rungrow-{tenant_hash}-{session_id}"

    @staticmethod
    def _livekit_token(api_key: str, api_secret: str, identity: str, room: str, now: datetime) -> str:
        claims = {
            "iss": api_key,
            "sub": identity,
            "nbf": int(now.timestamp()) - 5,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "video": {
                "roomJoin": True,
                "room": room,
                "canPublish": True,
                "canSubscribe": True,
                "canPublishData": True,
            },
        }
        return jwt.encode(claims, api_secret, algorithm="HS256")

    async def create_session(self, repo, principal, *, client_mode: str = "livekit", product_id: str | None = None):
        self._require(principal, "voice.use")
        self._require(principal, "executive.read")
        if product_id:
            from .main import authorize_product_access
            authorize_product_access(principal, product_id)

        session_id = str(uuid4())
        room_name = self._room(principal.tenant_id, session_id)
        api_url = os.getenv(LIVEKIT_URL_ENV, "").strip()
        api_key = os.getenv(LIVEKIT_API_KEY_ENV, "").strip()
        api_secret = os.getenv(LIVEKIT_API_SECRET_ENV, "").strip()
        now = self.clock()
        configured = bool(api_url and api_key and api_secret)
        if client_mode not in {"browser", "livekit"}:
            raise ValueError("client_mode must be browser or livekit")
        status = "ACTIVE" if configured or client_mode == "browser" else "BLOCKED_UNCONFIGURED"
        await repo.session.execute(text("""
            INSERT INTO voice_sessions
              (id, tenant_id, principal_id, principal_role, product_id, room_name,
               livekit_url, client_mode, status, identity_disclosure, created_at, expires_at)
            VALUES
              (:id, :tenant, :principal, :role, :product, :room, :url, :mode,
               :status, :disclosure, :created, :expires)
        """), {
            "id": session_id, "tenant": principal.tenant_id, "principal": principal.user_id,
            "role": principal.role, "product": product_id, "room": room_name,
            "url": api_url or None, "mode": client_mode, "status": status,
            "disclosure": AI_IDENTITY_DISCLOSURE, "created": now,
            "expires": now + timedelta(hours=1),
        })
        if not configured and client_mode == "livekit":
            raise LiveKitNotConfigured(session_id)
        token = self._livekit_token(api_key, api_secret, principal.user_id, room_name, now) if configured else None
        return {
            "session_id": session_id, "room_name": room_name, "livekit_url": api_url or None,
            "transport": "livekit" if configured else "browser_fallback",
            "participant_identity": principal.user_id, "participant_token": token,
            "token_expires_at": (now + timedelta(hours=1)).isoformat(),
            "identity_disclosure": AI_IDENTITY_DISCLOSURE, "status": status,
        }

    async def append_turn(self, repo, principal, session_id: str, transcript: str, *,
                          interrupted_at_ms: int | None = None, source: str = "web_speech"):
        self._require(principal, "voice.use")
        clean = (transcript or "").strip()
        if not clean or len(clean) > 8000:
            raise ValueError("Transcript must contain 1–8000 characters")
        if interrupted_at_ms is not None and interrupted_at_ms < 0:
            raise ValueError("interrupted_at_ms must be non-negative")
        session = (await repo.session.execute(text("""
            SELECT id, tenant_id, principal_id, product_id, status
            FROM voice_sessions WHERE id=:id AND tenant_id=:tenant
            FOR UPDATE
        """), {"id": session_id, "tenant": principal.tenant_id})).mappings().first()
        if not session:
            raise LookupError("Voice session not found")
        if session["principal_id"] != principal.user_id:
            raise VoiceSessionForbidden("Voice session belongs to another authenticated principal")
        if session["status"] != "ACTIVE":
            raise ValueError("Voice session is not active")
        history = (await repo.session.execute(text("""
            SELECT speaker, transcript FROM voice_turns
            WHERE session_id=:id AND tenant_id=:tenant
            ORDER BY created_at DESC LIMIT 12
        """), {"id": session_id, "tenant": principal.tenant_id})).mappings().all()
        turn_id = str(uuid4())
        await repo.session.execute(text("""
            INSERT INTO voice_turns
              (id, tenant_id, session_id, speaker, transcript, source,
               interrupted_at_ms, context_turn_count, created_at)
            VALUES (:id, :tenant, :session, 'FOUNDER', :transcript, :source,
                    :interrupted, :context_count, now())
        """), {"id": turn_id, "tenant": principal.tenant_id, "session": session_id,
               "transcript": clean, "source": source, "interrupted": interrupted_at_ms,
               "context_count": len(history)})
        return {
            "turn_id": turn_id, "session_id": session_id, "transcript": clean,
            "interrupted_at_ms": interrupted_at_ms,
            "context": [{"speaker": row["speaker"], "transcript": row["transcript"]}
                        for row in reversed(history)],
            "product_id": session["product_id"],
        }

    async def append_response(self, repo, principal, session_id: str, transcript: str):
        await repo.session.execute(text("""
            INSERT INTO voice_turns
              (id, tenant_id, session_id, speaker, transcript, source, created_at)
            VALUES (:id, :tenant, :session, 'CEO', :transcript, 'digital_ceo', now())
        """), {"id": str(uuid4()), "tenant": principal.tenant_id,
               "session": session_id, "transcript": transcript[:8000]})
