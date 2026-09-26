from pathlib import Path

import pytest

from backend_foundation.manifest import (
    ManifestError,
    load_catalog,
    load_profiles,
    load_project_manifest,
    resolve_manifest_modules,
    validate_profile,
)


def test_catalog_and_profiles_are_versioned() -> None:
    catalog = load_catalog()
    profiles = load_profiles()
    assert set(catalog) == {
        "database",
        "temporal",
        "jobs",
        "users",
        "rbac",
        "email",
        "auth",
        "storage",
        "ai",
    }
    assert set(profiles) == {"api", "workflow", "ai", "identity", "saas", "full"}
    assert validate_profile("workflow", catalog) == ("database", "temporal", "jobs")
    assert validate_profile("identity", catalog) == (
        "database",
        "temporal",
        "jobs",
        "users",
        "rbac",
        "email",
        "auth",
    )
    assert catalog["jobs"].migrations == ("migrations/jobs.py",)
    assert catalog["storage"].compose_files == ("minio",)


def test_unknown_module_and_dependency_cycle_are_rejected() -> None:
    with pytest.raises(ManifestError, match="Unknown module"):
        resolve_manifest_modules(("missing",), {})
    with pytest.raises(ManifestError, match="dependency cycle"):
        from backend_foundation.manifest import ModuleManifest

        catalog = {
            "a": ModuleManifest("a", "1", "", ("b",), (), "", False, False, False, False, False),
            "b": ModuleManifest("b", "1", "", ("a",), (), "", False, False, False, False, False),
        }
        resolve_manifest_modules(("a",), catalog)


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "module.toml"
    path.write_text("not = [", encoding="utf-8")
    with pytest.raises(ManifestError, match="Invalid TOML"):
        load_project_manifest(path)
