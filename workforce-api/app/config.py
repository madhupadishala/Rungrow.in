from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    # Empty by default: the API must not silently boot with a fictitious DB URL.
    database_url: str = ""
    jwt_secret: str = "local-development-secret-change-before-deploy"
    jwt_public_key: str = ""
    jwt_audience: str = "rungrow-workforce-api"
    jwt_issuer: str = "https://rungrow.in"
    access_token_minutes: int = 30
    cors_origins: str = "http://localhost:3000,https://rungrow.in"

    @property
    def allowed_origins(self):
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

settings = Settings()
