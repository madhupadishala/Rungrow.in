from __future__ import annotations

import os
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
    if values.get("JWT_SECRET", "").strip() in {
        "", "local-development-secret-change-before-deploy",
        "development-secret", "test-secret", "secret",
    }:
        # JWT_SECRET is not used for production verification; explicitly reject
        # known development/test material if an operator supplied it.
        if values.get("JWT_SECRET", "").strip():
            raise RuntimeError("Static development/test JWT secrets are forbidden in production")
