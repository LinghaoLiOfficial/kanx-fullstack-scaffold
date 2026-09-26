import pytest

from backend_foundation import dev
from backend_foundation.core.config import AppProfile, Settings


def test_compose_command_uses_project_and_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(command: list[str], **kwargs: object):
        calls.append((command, kwargs.get("env")))
        return object()

    monkeypatch.setattr(dev, "_run", fake_run)
    settings = Settings(_env_file=None, app_slug="demo", app_profile=AppProfile.WORKFLOW)
    dev._compose(settings, "up", "-d")
    command, environment = calls[0]
    assert command[:5] == ["docker", "compose", "-p", "demo", "-f"]
    assert "--profile" in command and "workflow" in command
    assert environment is not None
    assert environment["APP_SLUG"] == "demo"
    assert environment["TEMPORAL_NAMESPACE"] == "backend-foundation"


def test_api_profile_does_not_enable_temporal() -> None:
    assert not dev._profile_enabled(Settings(_env_file=None, app_profile=AppProfile.API))
    assert dev._profile_enabled(Settings(_env_file=None, app_profile=AppProfile.FULL))


def test_port_available_accepts_owned_container(monkeypatch: pytest.MonkeyPatch) -> None:
    class OccupiedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def bind(self, _address: tuple[str, int]) -> None:
            raise OSError("occupied")

    monkeypatch.setattr(dev.socket, "socket", lambda: OccupiedSocket())
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": "demo/postgres\n"})(),
    )
    dev.ensure_port_available(Settings(_env_file=None, app_slug="demo"), 5432, "postgres")


def test_missing_docker_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dev.shutil, "which", lambda command: None if command == "docker" else "/usr/bin/uv"
    )
    with pytest.raises(RuntimeError, match="docker"):
        dev.check_prerequisites()


def test_service_urls_follow_profile_and_ports() -> None:
    settings = Settings(
        _env_file=None,
        app_profile=AppProfile.FULL,
        api_port=18000,
        gradio_port=17860,
        database_host_port=15432,
        temporal_host_port=17233,
        temporal_ui_port=18233,
        email_smtp_host_port=11025,
        email_ui_port=18025,
        storage_host_port=19000,
        storage_console_port=19001,
    )
    urls = dict(dev.service_urls(settings))
    assert urls["API"] == "http://127.0.0.1:18000"
    assert urls["Gradio"] == "http://127.0.0.1:17860"
    assert urls["PostgreSQL"] == "postgresql://127.0.0.1:15432"
    assert urls["Temporal UI"] == "http://localhost:18233"
    assert urls["Mailpit UI"] == "http://127.0.0.1:18025"
    assert urls["MinIO Console"] == "http://127.0.0.1:19001"


def test_service_urls_do_not_include_credentials() -> None:
    settings = Settings(_env_file=None, app_profile=AppProfile.API)
    output = "\n".join(f"{label} {url}" for label, url in dev.service_urls(settings))
    assert "foundation" not in output
    assert "password" not in output.lower()
