from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    """Raised when a module or project manifest is invalid."""


@dataclass(frozen=True)
class ModuleManifest:
    name: str
    version: str
    description: str
    requires: tuple[str, ...]
    profiles: tuple[str, ...]
    optional_extra: str
    provides_router: bool
    provides_lifespan: bool
    provides_health_check: bool
    provides_workflows: bool
    provides_activities: bool
    model_imports: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    compose_files: tuple[str, ...] = ()
    python_dependencies: tuple[str, ...] = ()
    migrations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileManifest:
    name: str
    modules: tuple[str, ...]


@dataclass(frozen=True)
class ProjectManifest:
    composition_version: int
    name: str
    version: str
    description: str
    slug: str
    package: str
    profile: str
    modules: tuple[str, ...]


def _load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except FileNotFoundError as error:
        raise ManifestError(f"Manifest not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ManifestError(f"Invalid TOML manifest {path}: {error}") from error
    return value


def load_module_manifest(path: Path) -> ModuleManifest:
    data = _load(path)
    try:
        return ModuleManifest(
            name=str(data["name"]),
            version=str(data["version"]),
            description=str(data["description"]),
            requires=tuple(str(item) for item in data.get("requires", [])),
            profiles=tuple(str(item) for item in data.get("profiles", [])),
            optional_extra=str(data.get("optional_extra", "")),
            provides_router=bool(data.get("provides_router", False)),
            provides_lifespan=bool(data.get("provides_lifespan", False)),
            provides_health_check=bool(data.get("provides_health_check", False)),
            provides_workflows=bool(data.get("provides_workflows", False)),
            provides_activities=bool(data.get("provides_activities", False)),
            model_imports=tuple(str(item) for item in data.get("model_imports", [])),
            permissions=tuple(str(item) for item in data.get("permissions", [])),
            required_env=tuple(str(item) for item in data.get("required_env", [])),
            optional_env=tuple(str(item) for item in data.get("optional_env", [])),
            compose_files=tuple(str(item) for item in data.get("compose_files", [])),
            python_dependencies=tuple(str(item) for item in data.get("python_dependencies", [])),
            migrations=tuple(str(item) for item in data.get("migrations", [])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ManifestError(f"Invalid module manifest: {path}") from error


def load_profile_manifest(path: Path) -> ProfileManifest:
    data = _load(path)
    try:
        return ProfileManifest(str(data["name"]), tuple(str(item) for item in data["modules"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ManifestError(f"Invalid profile manifest: {path}") from error


def load_project_manifest(path: Path) -> ProjectManifest:
    data = _load(path)
    try:
        return ProjectManifest(
            composition_version=int(data["composition_version"]),
            name=str(data["name"]),
            version=str(data["version"]),
            description=str(data["description"]),
            slug=str(data["slug"]),
            package=str(data["package"]),
            profile=str(data["profile"]),
            modules=tuple(str(item) for item in data["modules"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ManifestError(f"Invalid project manifest: {path}") from error


def template_root() -> Path:
    candidates = (
        Path(__file__).resolve().parents[2] / "templates",
        Path(__file__).resolve().parent / "templates",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise ManifestError("Template catalog is not installed")


def load_catalog() -> dict[str, ModuleManifest]:
    modules_dir = template_root() / "backend" / "modules"
    catalog = {
        path.parent.name: load_module_manifest(path) for path in modules_dir.glob("*/module.toml")
    }
    for directory_name, manifest in catalog.items():
        if directory_name != manifest.name:
            raise ManifestError(
                f"Module directory {directory_name!r} does not match "
                f"manifest name {manifest.name!r}"
            )
        module_root = modules_dir / directory_name
        missing_migrations = [
            item for item in manifest.migrations if not (module_root / item).is_file()
        ]
        if missing_migrations:
            raise ManifestError(
                f"Module {manifest.name!r} references missing migrations: "
                f"{', '.join(missing_migrations)}"
            )
        if manifest.provides_router and not (module_root / "files").is_dir():
            raise ManifestError(f"Module {manifest.name!r} declares a router without files")
    return catalog


def load_profiles() -> dict[str, ProfileManifest]:
    profiles_dir = template_root() / "profiles"
    profiles = {
        path.parent.name: load_profile_manifest(path)
        for path in profiles_dir.glob("*/profile.toml")
    }
    for directory_name, manifest in profiles.items():
        if directory_name != manifest.name:
            raise ManifestError(
                f"Profile directory {directory_name!r} does not match "
                f"manifest name {manifest.name!r}"
            )
    return profiles


def validate_profile(
    profile: str, catalog: dict[str, ModuleManifest] | None = None
) -> tuple[str, ...]:
    catalog = catalog or load_catalog()
    profiles = load_profiles()
    if profile not in profiles:
        raise ManifestError(f"Unknown profile: {profile}")
    resolved = resolve_manifest_modules(profiles[profile].modules, catalog)
    unsupported = [name for name in resolved if profile not in catalog[name].profiles]
    if unsupported:
        raise ManifestError(
            f"Profile {profile!r} is not supported by module(s): {', '.join(unsupported)}"
        )
    return resolved


def resolve_manifest_modules(
    requested: tuple[str, ...], catalog: dict[str, ModuleManifest]
) -> tuple[str, ...]:
    resolved: list[str] = []
    visiting: list[str] = []
    completed: set[str] = set()

    def visit(name: str) -> None:
        if name in completed:
            return
        if name in visiting:
            raise ManifestError(f"Module dependency cycle: {' -> '.join([*visiting, name])}")
        if name not in catalog:
            raise ManifestError(f"Unknown module: {name}")
        visiting.append(name)
        for dependency in catalog[name].requires:
            visit(dependency)
        visiting.pop()
        completed.add(name)
        resolved.append(name)

    for name in requested:
        visit(name)
    return tuple(resolved)
