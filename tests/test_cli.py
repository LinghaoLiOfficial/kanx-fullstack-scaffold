import subprocess
from pathlib import Path

import pytest

from backend_foundation.cli import (
    CliError,
    _cleanup_downloads,
    _context,
    create_project,
    doctor,
    main,
    package_name,
    slugify,
)


def test_slug_and_package_names() -> None:
    assert slugify(" My Service ") == "my-service"
    assert package_name("my-service") == "my_service"
    with pytest.raises(CliError):
        slugify("---")


@pytest.mark.parametrize("profile", ["api", "workflow", "ai", "full"])
def test_new_generates_runnable_project(tmp_path: Path, profile: str) -> None:
    destination = tmp_path / profile
    assert create_project(f"Demo {profile}", profile, destination) == destination.resolve()
    assert (destination / "backend" / "module.toml").exists()
    assert (destination / "backend" / "compose.yml").exists()
    assert (destination / "frontend" / "package.json").exists()
    assert (
        destination / "backend" / "src" / package_name(f"demo-{profile}") / "core" / "logging.py"
    ).exists()
    assert (destination / "frontend" / ".env").read_text() == (
        destination / "frontend" / ".env.example"
    ).read_text()
    assert doctor(destination) == 0


def test_ai_project_configures_gradio_bind_address(tmp_path: Path) -> None:
    destination = create_project("Demo AI", "ai", tmp_path / "demo-ai")
    env_example = (destination / "backend" / ".env.example").read_text(encoding="utf-8")
    gradio_app = (destination / "backend" / "src" / "demo_ai" / "gradio_app.py").read_text(
        encoding="utf-8"
    )

    assert "GRADIO_HOST=127.0.0.1" in env_example
    assert "GRADIO_PORT=7860" in env_example
    assert "server_name=settings.gradio_host" in gradio_app
    assert "server_port=settings.gradio_port" in gradio_app
    assert "AI_TASK_CONFIGS={}" in env_example


def test_generated_identity_runtime_contract(tmp_path: Path) -> None:
    destination = create_project("Identity Contract", "identity", tmp_path / "identity-contract")
    router = (
        destination / "backend" / "src" / "identity_contract" / "modules" / "auth" / "router.py"
    ).read_text(encoding="utf-8")
    pyproject = (destination / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'path="/auth"' in router
    assert 'path="/"' in router
    assert "cryptography>=45,<47" in pyproject


def test_generated_project_ignores_local_environment_files(tmp_path: Path) -> None:
    destination = create_project("Secret Safe", "ai", tmp_path / "secret-safe")
    subprocess.run(["git", "init", "--quiet"], cwd=destination, check=True)
    for name in (".env", ".env.local", ".env.production"):
        (destination / "backend" / name).write_text(
            "AI_TASK_CONFIGS__SUMMARIZE__API_KEY=real-secret\n",
            encoding="utf-8",
        )
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", name],
            cwd=destination / "backend",
            check=False,
        )
        assert ignored.returncode == 0, f"{name} must be ignored"

    example = subprocess.run(
        ["git", "check-ignore", "--quiet", ".env.example"],
        cwd=destination / "backend",
        check=False,
    )
    assert example.returncode == 1


def test_new_rejects_non_empty_directory(tmp_path: Path) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(CliError, match="not empty"):
        create_project("example", "api", destination)


def test_main_unknown_command_returns_nonzero() -> None:
    with pytest.raises(SystemExit):
        main(["add", "missing", "--project", "/tmp/missing"])


def test_custom_profile_project_is_valid(tmp_path: Path) -> None:
    destination = create_project(
        "Custom Test",
        "custom",
        tmp_path / "custom-test",
        ("database",),
    )
    manifest = (destination / "backend" / "module.toml").read_text(encoding="utf-8")
    assert 'profile = "custom"' in manifest
    assert doctor(destination) == 0


def test_context_rejects_unknown_profile_and_missing_database() -> None:
    with pytest.raises(CliError, match="Unknown profile"):
        _context("example", "missing")
    context, modules = _context("example", "custom", ("ai",))
    assert modules == ("database", "ai")
    assert context["PROFILE"] == "custom"


def test_download_cleanup_removes_old_and_excess_archives(tmp_path: Path, monkeypatch) -> None:
    import os
    import time

    monkeypatch.setattr("backend_foundation.cli.DOWNLOAD_DIR", tmp_path)
    old = tmp_path / "old.zip"
    old.write_bytes(b"old")
    old_timestamp = time.time() - 7200
    os.utime(old, (old_timestamp, old_timestamp))
    for index in range(21):
        (tmp_path / f"archive-{index}.zip").write_bytes(b"zip")
    _cleanup_downloads()
    assert not old.exists()
    assert len(list(tmp_path.glob("*.zip"))) == 20
