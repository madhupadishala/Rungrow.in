"""Transactional, checksum-verified PostgreSQL migration runner.

Run with: python -m app.migrate
The legacy 001_workforce.sql bootstrap is intentionally excluded; the canonical
chain starts at 001_initial_workforce_schema.sql. New numbered migrations (including
010_workforce_2_orchestration.sql) are discovered automatically and checksum-verified.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_LOCK_KEY = 731_904_007
VERSIONED_SQL = re.compile(r"^(?P<version>\d{3})_(?!workforce\.sql$).+\.sql$")


class MigrationDriftError(RuntimeError):
    """An already-applied migration no longer matches its recorded checksum."""


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, Path, str]]:
    files = []
    for path in directory.iterdir():
        match = VERSIONED_SQL.match(path.name)
        if not match or path.name == "000_schema_migrations.sql":
            continue
        files.append((match.group("version"), path, hashlib.sha256(path.read_bytes()).hexdigest()))
    files.sort(key=lambda item: (item[0], item[1].name))
    versions = [item[0] for item in files]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Migration versions must be unique")
    if not files:
        raise RuntimeError(f"No versioned SQL migrations found in {directory}")
    return files


def asyncpg_dsn(database_url: str) -> tuple[str, str | None]:
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to run migrations")
    normalized = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parts = urlsplit(normalized)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    dsn = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    ssl = "require" if sslmode in {"require", "verify-ca", "verify-full"} else None
    return dsn, ssl


async def apply_migrations(connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    bootstrap = directory / "000_schema_migrations.sql"
    if not bootstrap.is_file():
        raise RuntimeError("000_schema_migrations.sql is required")
    # Bootstrap itself is intentionally small and safe to execute repeatedly.
    async with connection.transaction():
        await connection.execute(bootstrap.read_text(encoding="utf-8"))

    applied_now: list[str] = []
    for version, path, checksum in migration_files(directory):
        existing = await connection.fetchrow(
            "SELECT filename, checksum_sha256 FROM schema_migrations WHERE version = $1",
            version,
        )
        if existing:
            if existing["filename"] != path.name or existing["checksum_sha256"] != checksum:
                raise MigrationDriftError(
                    f"Applied migration {version} checksum/name drift: "
                    f"recorded={existing['filename']}:{existing['checksum_sha256']} "
                    f"current={path.name}:{checksum}"
                )
            continue

        sql = path.read_text(encoding="utf-8")
        async with connection.transaction():
            # Record is committed atomically with the migration DDL/data changes.
            await connection.execute(sql)
            await connection.execute(
                """INSERT INTO schema_migrations(version, filename, checksum_sha256)
                   VALUES ($1, $2, $3)""",
                version, path.name, checksum,
            )
        applied_now.append(path.name)
    return applied_now


async def run(database_url: str | None = None) -> list[str]:
    dsn, ssl = asyncpg_dsn(database_url or os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL", ""))
    connection = await asyncpg.connect(dsn=dsn, ssl=ssl)
    locked = False
    try:
        # A session advisory lock serializes concurrent deploys across API/worker services.
        await connection.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
        locked = True
        applied = await apply_migrations(connection)
        print("Migration chain verified; newly applied: " + (", ".join(applied) if applied else "none"))
        return applied
    finally:
        try:
            if locked:
                await connection.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
        finally:
            await connection.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
