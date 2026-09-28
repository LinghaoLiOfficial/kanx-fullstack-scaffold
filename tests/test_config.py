import pytest
from pydantic import ValidationError

from backend_foundation.core.config import AppEnvironment, AppProfile, Settings


def test_settings_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.api_port == 8000
    assert settings.app_profile is AppProfile.API
    assert settings.temporal_namespace == "backend-foundation"
    assert "55432" in settings.database_url
    assert settings.database_host_port == 55432
    assert settings.docker_project_name == "backend-foundation"
    assert settings.temporal_ui_url == "http://localhost:8233"
    assert settings.temporal_worker_processes == 1
    assert settings.temporal_max_concurrent_activities == 100
    assert settings.auto_register_namespace
    assert not Settings(_env_file=None, app_env="production").auto_register_namespace


def test_settings_reject_invalid_port() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_port=0)


def test_temporal_worker_concurrency_can_be_configured_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_WORKER_PROCESSES", "3")
    monkeypatch.setenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "24")
    settings = Settings(_env_file=None)
    assert settings.temporal_worker_processes == 3
    assert settings.temporal_max_concurrent_activities == 24


@pytest.mark.parametrize(
    "field",
    ("temporal_worker_processes", "temporal_max_concurrent_activities"),
)
def test_temporal_worker_concurrency_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_settings_reject_invalid_slug() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_slug="Not Valid")


def test_compose_project_can_be_overridden() -> None:
    settings = Settings(_env_file=None, app_slug="demo", compose_project_name="shared-demo")
    assert settings.docker_project_name == "shared-demo"


def test_environment_aware_dotenv_loading(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("API_PORT=9001\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("API_PORT=9002\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "local")
    assert Settings().api_port == 9002
    monkeypatch.setenv("API_PORT", "9003")
    assert Settings().api_port == 9003
    monkeypatch.delenv("API_PORT")
    monkeypatch.setenv("APP_ENV", "staging")
    settings = Settings()
    assert settings.app_env is AppEnvironment.STAGING
    assert settings.api_port == 8000


def test_development_environment_migrates_to_local() -> None:
    assert Settings(_env_file=None, app_env="development").app_env is AppEnvironment.LOCAL
