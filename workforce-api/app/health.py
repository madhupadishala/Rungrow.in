"""Liveness/readiness probes tied to the checksum-verified migration chain."""
from __future__ import annotations
import os
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from .config import settings
from .migrate import migration_files
from .repositories import session_factory

router=APIRouter(tags=["health"])

@router.get("/health/live")
async def live():
    return {"status":"alive","service":"rungrow-workforce-api"}

@router.get("/health/ready")
async def ready():
    checks={}
    failures=[]
    if not settings.database_url:
        checks["postgresql"]={"ok":False,"reason":"DATABASE_URL is not configured"}
        failures.append("postgresql")
    else:
        try:
            async with session_factory()() as session:
                await session.execute(text("SELECT 1"))
                rows=(await session.execute(text(
                    "SELECT version,filename,checksum_sha256 FROM schema_migrations ORDER BY version"
                ))).mappings().all()
            actual={str(r["version"]):(r["filename"],r["checksum_sha256"]) for r in rows}
            expected={version:(path.name,checksum) for version,path,checksum in migration_files()}
            missing=sorted(set(expected)-set(actual))
            drift=sorted(v for v in set(expected)&set(actual) if expected[v]!=actual[v])
            extra=sorted(set(actual)-set(expected))
            ok=not missing and not drift and not extra
            checks["postgresql"]={"ok":True}
            checks["migrations"]={"ok":ok,"expected_versions":sorted(expected),
                "missing":missing,"checksum_or_filename_drift":drift,"unexpected":extra}
            if not ok: failures.append("migrations")
        except Exception as exc:
            checks["postgresql"]={"ok":False,"reason":type(exc).__name__}
            checks["migrations"]={"ok":False,"reason":"Database migration verification unavailable"}
            failures.extend(["postgresql","migrations"])
    production=settings.app_env=="production"
    persistent=bool(settings.database_url) and os.getenv("WORKFORCE_REPOSITORY","postgres").lower() not in {"memory","inmemory"}
    checks["repository"]={"ok":persistent,"type":"postgresql" if persistent else "in_memory_or_unconfigured"}
    if production and not persistent: failures.append("repository")
    oidc=bool(os.getenv("JWT_JWKS_URL") or os.getenv("JWT_PUBLIC_KEY") or settings.jwt_jwks_url or settings.jwt_public_key)
    service_secret=len(os.getenv("STUDIO_GROWTH_OS_SERVICE_SECRET","").encode())>=32
    webhook_secret=len(os.getenv("RUNGROW_ADAPTER_WEBHOOK_SECRET","").encode())>=32
    checks["oidc"]={"ok":oidc}
    checks["hmac"]={"ok":service_secret and webhook_secret,
        "service_secret_configured":service_secret,"webhook_secret_configured":webhook_secret}
    if production and not oidc: failures.append("oidc")
    if production and not (service_secret and webhook_secret): failures.append("hmac")
    if failures:
        return JSONResponse(status_code=503,content={"status":"not_ready","checks":checks,"failed_checks":sorted(set(failures))})
    return {"status":"ready","checks":checks,"failed_checks":[]}
