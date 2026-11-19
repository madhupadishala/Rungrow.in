from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import jwt
import pytest

from app import migrate
from app.config import validate_production_environment
from app.security import _decode_production_token, issue_access_token
from app.config import settings


class FakeConnection:
    def __init__(self):
        self.records = {}
        self.executed = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql, *args):
        self.executed.append(sql)
        if "INSERT INTO schema_migrations" in sql:
            version, filename, checksum = args
            self.records[version] = {
                "filename": filename,
                "checksum_sha256": checksum,
            }
        return "OK"

    async def fetchrow(self, sql, version):
        return self.records.get(version)


def make_migration_dir(tmp_path: Path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "000_schema_migrations.sql").write_text(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version text primary key);",
        encoding="utf-8",
    )
    (directory / "001_first.sql").write_text(
        "CREATE TABLE first_table(id integer primary key);", encoding="utf-8"
    )
    (directory / "002_second.sql").write_text(
        "CREATE TABLE second_table(id integer primary key);", encoding="utf-8"
    )
    # Legacy pre-tenant bootstrap is never part of the canonical ordered chain.
    (directory / "001_workforce.sql").write_text("SELECT 'legacy';", encoding="utf-8")
    return directory


@pytest.mark.asyncio
async def test_migration_runner_is_ordered_and_idempotent(tmp_path):
    directory = make_migration_dir(tmp_path)
    connection = FakeConnection()

    first = await migrate.apply_migrations(connection, directory)
    second = await migrate.apply_migrations(connection, directory)

    assert first == ["001_first.sql", "002_second.sql"]
    assert second == []
    assert list(connection.records) == ["001", "002"]


@pytest.mark.asyncio
async def test_migration_runner_fails_closed_on_checksum_drift(tmp_path):
    directory = make_migration_dir(tmp_path)
    connection = FakeConnection()
    await migrate.apply_migrations(connection, directory)

    (directory / "001_first.sql").write_text(
        "DROP TABLE first_table;", encoding="utf-8"
    )
    with pytest.raises(migrate.MigrationDriftError, match="checksum/name drift"):
        await migrate.apply_migrations(connection, directory)


def test_production_startup_rejects_missing_required_secrets():
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        validate_production_environment({"APP_ENV": "production"})


def test_production_startup_rejects_static_development_jwt_secret():
    env = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql://user:pass@host/db?sslmode=require",
        "JWT_JWKS_URL": "https://issuer.example/.well-known/jwks.json",
        "STUDIO_GROWTH_OS_SERVICE_SECRET": "a-real-secret-value-with-at-least-32-bytes",
        "RUNGROW_ADAPTER_WEBHOOK_SECRET": "another-real-secret-value-with-at-least-32-bytes",
        "JWT_SECRET": "local-development-secret-change-before-deploy",
    }
    with pytest.raises(RuntimeError, match="Static development/test JWT"):
        validate_production_environment(env)


def test_production_jwt_verifier_rejects_hs256_even_when_signed_correctly(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    token = jwt.encode(
        {"sub": "user", "tid": "tenant", "role": "founder",
         "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "exp": 4102444800},
        "test-secret", algorithm="HS256",
    )
    with pytest.raises(Exception, match="RS256 or ES256"):
        _decode_production_token(token)


def test_local_hs256_token_issuer_is_disabled_in_production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    with pytest.raises(RuntimeError, match="disabled in production"):
        issue_access_token("user", "tenant", "founder", ["*"])
