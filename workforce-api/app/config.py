from __future__ import annotations

import os
from urllib.parse import urlsplit
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    database_url: str = ""
    # Development/test only. Production verification is asymmetric RS256/ES256.
    jwt_secret: str = "local-development-secret-change-before-deploy"
    jwt_public_key: str = ""
    jwt_jwks_url: str = ""
    jwt_audience: str = "rungrow-workforce-api"
    jwt_issuer: str = "https://rungrow.in"
    access_token_minutes: int = 30
    cors_origins: str = "http://localhost:3000,https://rungrow.in"

    @property
    def allowed_origins(self):
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]


settings = Settings()


def validate_production_environment(env: dict[str, str] | None = None) -> None:
    values = os.environ if env is None else env
    if values.get("APP_ENV", "development").lower() != "production":
        return
    required = ("DATABASE_URL",)
    missing = [key for key in required if not values.get(key, "").strip()]
    if not values.get("JWT_JWKS_URL", "").strip() and not values.get("JWT_PUBLIC_KEY", "").strip():
        missing.append("JWT_JWKS_URL or JWT_PUBLIC_KEY")
    if not values.get("STUDIO_GROWTH_OS_SERVICE_SECRET", "").strip():
        missing.append("STUDIO_GROWTH_OS_SERVICE_SECRET")
    if not values.get("RUNGROW_ADAPTER_WEBHOOK_SECRET", "").strip():
        missing.append("RUNGROW_ADAPTER_WEBHOOK_SECRET")
    if missing:
        raise RuntimeError("Production configuration is incomplete: " + ", ".join(missing))
    database = urlsplit(values["DATABASE_URL"])
    if database.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"} or not database.hostname:
        raise RuntimeError("Production DATABASE_URL must be a valid PostgreSQL connection URL")
    db_query = dict(__import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(database.query))
    if db_query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise RuntimeError("Production PostgreSQL connections must require TLS (sslmode=require)")
    jwks_url = values.get("JWT_JWKS_URL", "").strip()
    if jwks_url:
        parsed_jwks = urlsplit(jwks_url)
        if parsed_jwks.scheme != "https" or not parsed_jwks.hostname:
            raise RuntimeError("JWT_JWKS_URL must be an HTTPS URL")
    else:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec, rsa
            public_key = serialization.load_pem_public_key(
                values["JWT_PUBLIC_KEY"].replace("\\n", "\n").encode("utf-8")
            )
        except Exception as exc:
            raise RuntimeError("JWT_PUBLIC_KEY must be a valid asymmetric PEM public key") from exc
        if not isinstance(public_key, (rsa.RSAPublicKey, ec.EllipticCurvePublicKey)):
            raise RuntimeError("JWT_PUBLIC_KEY must be RSA or EC; symmetric keys are forbidden")
    for key in ("STUDIO_GROWTH_OS_SERVICE_SECRET", "RUNGROW_ADAPTER_WEBHOOK_SECRET"):
        if len(values.get(key, "").encode("utf-8")) < 32:
            raise RuntimeError(f"{key} must contain at least 32 bytes")
    if values.get("JWT_SECRET", "").strip() in {
        "local-development-secret-change-before-deploy",
        "development-secret", "test-secret", "secret",
    }:
        raise RuntimeError("Static development/test JWT secrets are forbidden in production")
