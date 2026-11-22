import os
from uuid import uuid4

import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.policy import Principal
from app.repositories import PostgresWorkforceRepository
from app.voice_ceo import (
    AI_IDENTITY_DISCLOSURE, LiveKitNotConfigured, VoiceCEOSessionManager,
    VoiceSessionForbidden,
)

DB_URL = os.getenv("RUNGROW_TEST_DATABASE_URL", "")


@pytest.mark.asyncio
async def test_postgres16_voice_session_fails_closed_without_livekit_and_persists_blocked_row(monkeypatch):
    assert DB_URL, "CI must supply PostgreSQL 16 RUNGROW_TEST_DATABASE_URL"
    for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(key, raising=False)
    engine = create_async_engine(DB_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "voice-offline-" + uuid4().hex
    principal = Principal("oidc-founder-sub", tenant, "founder",
                          frozenset({"voice.use", "executive.read"}))
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal) as repo:
                with pytest.raises(LiveKitNotConfigured) as error:
                    await VoiceCEOSessionManager().create_session(repo, principal)
                row = await repo._one("SELECT status, principal_id, identity_disclosure FROM voice_sessions WHERE id=:id",
                                      {"id": error.value.session_id})
                assert row["status"] == "BLOCKED_UNCONFIGURED"
                assert row["principal_id"] == principal.user_id
                assert "AI Digital CEO" in row["identity_disclosure"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres16_voice_session_mints_tenant_bound_livekit_jwt_and_turn_context(monkeypatch):
    assert DB_URL, "CI must supply PostgreSQL 16 RUNGROW_TEST_DATABASE_URL"
    monkeypatch.setenv("LIVEKIT_URL", "wss://voice.example.test")
    monkeypatch.setenv("LIVEKIT_API_KEY", "ci-livekit-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "ci-livekit-secret-for-tests")
    engine = create_async_engine(DB_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "voice-live-" + uuid4().hex
    principal = Principal("oidc-founder-sub", tenant, "founder",
                          frozenset({"voice.use", "executive.read", "work.assign"}))
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal) as repo:
                manager = VoiceCEOSessionManager()
                created = await manager.create_session(repo, principal)
                claims = jwt.decode(created["participant_token"], "ci-livekit-secret-for-tests",
                                    algorithms=["HS256"], options={"verify_exp": False})
                assert claims["sub"] == principal.user_id
                assert claims["video"]["room"] == created["room_name"]
                assert claims["video"]["roomJoin"] is True
                assert claims["video"]["canPublish"] is True
                first = await manager.append_turn(repo, principal, created["session_id"],
                    "Give me the executive briefing", source="test_fixture")
                await manager.append_response(repo, principal, created["session_id"],
                    "I am RunGrow's AI Digital CEO. Revenue and capital baselines are each five crore.")
                second = await manager.append_turn(repo, principal, created["session_id"],
                    "Stop. I have a new instruction.", interrupted_at_ms=840, source="test_fixture")
                assert first["context"] == []
                assert second["interrupted_at_ms"] == 840
                assert second["context"][-1]["speaker"] == "CEO"
                rows = await repo._rows("SELECT speaker, interrupted_at_ms FROM voice_turns WHERE session_id=:id ORDER BY created_at",
                                        {"id": created["session_id"]})
                assert [x["speaker"] for x in rows] == ["FOUNDER", "CEO", "FOUNDER"]
                assert rows[-1]["interrupted_at_ms"] == 840
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres16_voice_rls_blocks_cross_tenant_session_and_turn_reads(monkeypatch):
    assert DB_URL, "CI must supply PostgreSQL 16 RUNGROW_TEST_DATABASE_URL"
    monkeypatch.setenv("LIVEKIT_URL", "wss://voice.example.test")
    monkeypatch.setenv("LIVEKIT_API_KEY", "ci-livekit-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "ci-livekit-secret-for-tests")
    engine = create_async_engine(DB_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = "voice-a-" + uuid4().hex, "voice-b-" + uuid4().hex
    founder_a = Principal("founder-a", tenant_a, "founder", frozenset({"voice.use", "executive.read"}))
    founder_b = Principal("founder-b", tenant_b, "founder", frozenset({"voice.use", "executive.read"}))
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session, founder_a) as repo:
                created = await VoiceCEOSessionManager().create_session(repo, founder_a)
                await VoiceCEOSessionManager().append_turn(repo, founder_a, created["session_id"],
                    "Private founder instruction")
        async with factory() as session:
            async with PostgresWorkforceRepository(session, founder_b) as repo:
                assert await repo._one("SELECT id FROM voice_sessions WHERE id=:id",
                                       {"id": created["session_id"]}) is None
                assert await repo._rows("SELECT id FROM voice_turns WHERE session_id=:id",
                                        {"id": created["session_id"]}) == []
                with pytest.raises(LookupError):
                    await VoiceCEOSessionManager().append_turn(repo, founder_b, created["session_id"],
                        "Attempt cross-tenant access")
    finally:
        await engine.dispose()


def test_voice_session_requires_voice_rbac():
    principal = Principal("reader", "tenant", "reader", frozenset({"executive.read"}))
    with pytest.raises(VoiceSessionForbidden):
        VoiceCEOSessionManager._require(principal, "voice.use")
