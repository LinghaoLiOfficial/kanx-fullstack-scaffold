from zipfile import ZipFile

import pytest

from backend_foundation import cli
from backend_foundation.gradio_app import (
    _effective_profile,
    _selected_modules,
    build_demo,
    generate_project_zip,
    run_smoke,
)


@pytest.mark.asyncio
async def test_run_smoke() -> None:
    assert await run_smoke(" hello ") == "hello-graph"
    assert await run_smoke("  ") == "请输入测试内容"


def test_build_demo() -> None:
    demo = build_demo()
    assert demo is not None


def test_generate_project_zip_is_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "DOWNLOAD_DIR", tmp_path / "downloads")
    status, archive, modules = generate_project_zip("Zip Demo", "api", [])
    assert archive is not None
    assert "生成成功" in status
    assert modules == "已安装模块：`database`"
    with ZipFile(archive) as package:
        names = package.namelist()
        assert "zip-demo/backend/module.toml" in names
        assert "zip-demo/frontend/.gitignore" in names
        assert "zip-demo/backend/.github/workflows/secrets.yml" not in names
        assert "zip-demo/backend/.env" in names
        assert "zip-demo/frontend/.env" in names
        assert not any("/.git/" in name for name in names)
    _, second_archive, _ = generate_project_zip("Zip Demo", "api", [])
    assert second_archive is not None
    assert second_archive != archive


def test_generate_project_zip_reports_invalid_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "DOWNLOAD_DIR", tmp_path / "downloads")
    status, archive, modules = generate_project_zip("---", "api", [])
    assert "生成失败" in status
    assert archive is None
    assert modules == ""


def test_generator_resolves_profile_modules() -> None:
    assert _selected_modules("api", ["temporal"]) == ("database", "temporal", "jobs")
    assert _effective_profile(("database", "temporal", "jobs")) == "workflow"
