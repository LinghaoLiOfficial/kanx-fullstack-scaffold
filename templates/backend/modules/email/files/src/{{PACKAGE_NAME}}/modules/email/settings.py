from functools import lru_cache

from pydantic import Field, SecretStr

from ...core.config import EnvironmentSettings


class EmailSettings(EnvironmentSettings):
    model_config = EnvironmentSettings.model_config | {"env_prefix": "EMAIL_"}

    smtp_host: str = "localhost"
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    use_tls: bool = False
    from_address: str = "no-reply@example.local"
    public_base_url: str = "http://localhost:3000"


@lru_cache
def get_email_settings() -> EmailSettings:
    return EmailSettings()
