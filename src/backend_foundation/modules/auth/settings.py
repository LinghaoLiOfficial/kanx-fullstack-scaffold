from functools import lru_cache

from pydantic import Field, SecretStr, model_validator

from ...core.config import AppEnvironment, EnvironmentSettings, configured_environment


class AuthSettings(EnvironmentSettings):
    model_config = EnvironmentSettings.model_config | {"env_prefix": "AUTH_"}

    jwt_secret: SecretStr = SecretStr("development-only-change-me-32-bytes")
    jwt_issuer: str = "backend-foundation"
    jwt_audience: str = "backend-foundation-api"
    access_token_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_days: int = Field(default=30, ge=1, le=365)
    allowed_origins: str = "http://localhost:3000"
    secure_cookies: bool = False

    @model_validator(mode="after")
    def validate_deployment_security(self) -> "AuthSettings":
        if configured_environment() in (AppEnvironment.STAGING, AppEnvironment.PRODUCTION):
            secret = self.jwt_secret.get_secret_value()
            if len(secret) < 32 or secret == "development-only-change-me-32-bytes":
                raise ValueError("deployed AUTH_JWT_SECRET must be unique and at least 32 chars")
            if not self.secure_cookies:
                raise ValueError("deployed environments require AUTH_SECURE_COOKIES=true")
            if any(
                origin.strip().startswith("http://") for origin in self.allowed_origins.split(",")
            ):
                raise ValueError("deployed AUTH_ALLOWED_ORIGINS must use HTTPS")
        return self


@lru_cache
def get_auth_settings() -> AuthSettings:
    return AuthSettings()
