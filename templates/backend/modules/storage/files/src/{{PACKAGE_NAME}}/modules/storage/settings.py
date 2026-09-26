from functools import lru_cache

from pydantic import Field, SecretStr, model_validator

from ...core.config import AppEnvironment, EnvironmentSettings, configured_environment


class StorageSettings(EnvironmentSettings):
    model_config = EnvironmentSettings.model_config | {"env_prefix": "STORAGE_"}

    endpoint_url: str = "http://localhost:9000"
    access_key: SecretStr = SecretStr("foundation")
    secret_key: SecretStr = SecretStr("foundation-secret")
    bucket: str = "backend-foundation"
    region: str = "us-east-1"
    presign_seconds: int = Field(default=900, ge=1, le=86400)
    max_file_size: int = Field(default=50 * 1024 * 1024, ge=1)
    allowed_content_types: str = "image/png,image/jpeg,video/mp4,application/pdf,text/plain"
    auto_create_bucket: bool = True
    multipart_threshold: int = Field(default=32 * 1024 * 1024, ge=5 * 1024 * 1024)
    multipart_part_size: int = Field(default=16 * 1024 * 1024, ge=5 * 1024 * 1024)
    multipart_expiry_hours: int = Field(default=24, ge=1, le=168)
    media_probe_timeout_seconds: int = Field(default=15, ge=1, le=120)
    max_image_pixels: int = Field(default=100_000_000, ge=1)
    cdn_base_url: str | None = None
    cdn_signing_secret: SecretStr = SecretStr("")
    event_webhook_secret: SecretStr = SecretStr("")
    event_queue_url: str | None = None

    @model_validator(mode="after")
    def validate_deployment_security(self) -> "StorageSettings":
        if configured_environment() in (AppEnvironment.STAGING, AppEnvironment.PRODUCTION):
            if self.auto_create_bucket:
                raise ValueError("deployed environments require STORAGE_AUTO_CREATE_BUCKET=false")
            if self.access_key.get_secret_value() == "foundation":
                raise ValueError("deployed storage credentials may not use local defaults")
            if self.cdn_base_url and not self.cdn_base_url.startswith("https://"):
                raise ValueError("deployed STORAGE_CDN_BASE_URL must use HTTPS")
        return self


@lru_cache
def get_storage_settings() -> StorageSettings:
    return StorageSettings()
