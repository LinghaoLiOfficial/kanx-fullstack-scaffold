from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI

from backend_foundation.core.config import AppProfile, Settings
from backend_foundation.core.health import Check

ModuleLifespan = Callable[[FastAPI, Settings], AbstractAsyncContextManager[None]]
HealthChecks = Callable[[FastAPI], Mapping[str, Check]]
JobHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
SettingsFactory = Callable[[], Any]


@dataclass(frozen=True)
class MigrationDescriptor:
    name: str


@dataclass(frozen=True)
class ComposeCapability:
    name: str


@dataclass(frozen=True)
class JobTypeSpec:
    name: str
    handler: JobHandler
    version: str = "1.0.0"
    timeout_seconds: int = 300
    maximum_attempts: int = 5
    initial_backoff_seconds: float = 1.0
    maximum_backoff_seconds: float = 300.0
    backoff_coefficient: float = 2.0
    default_priority: str = "normal"
    concurrency_limit: int | None = None


JobHandlerSpec = JobTypeSpec


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    requires: tuple[str, ...] = ()
    routers: tuple[APIRouter, ...] = ()
    lifespan: ModuleLifespan | None = None
    health_checks: HealthChecks | None = None
    workflows: tuple[Any, ...] = ()
    activities: tuple[Any, ...] = ()
    models: tuple[type[Any], ...] = ()
    migrations: tuple[MigrationDescriptor, ...] = ()
    permissions: tuple[str, ...] = ()
    job_handlers: tuple[JobTypeSpec, ...] = ()
    settings_factory: SettingsFactory | None = None
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = ()
    compose_capabilities: tuple[ComposeCapability, ...] = ()


class ModuleConfigurationError(RuntimeError):
    """Raised when an explicitly composed module graph is invalid."""


PROFILE_MODULES: dict[AppProfile, tuple[str, ...]] = {
    AppProfile.API: ("database",),
    AppProfile.WORKFLOW: ("database", "temporal", "jobs"),
    AppProfile.AI: ("database", "ai"),
    AppProfile.IDENTITY: ("database", "temporal", "jobs", "users", "rbac", "email", "auth"),
    AppProfile.SAAS: ("database", "temporal", "jobs", "users", "rbac", "email", "auth", "storage"),
    AppProfile.FULL: (
        "database",
        "temporal",
        "jobs",
        "users",
        "rbac",
        "email",
        "auth",
        "storage",
        "ai",
    ),
}


def resolve_modules(
    requested: tuple[str, ...], registry: Mapping[str, ModuleSpec]
) -> tuple[ModuleSpec, ...]:
    resolved: list[ModuleSpec] = []
    completed: set[str] = set()
    visiting: list[str] = []

    def visit(name: str) -> None:
        if name in completed:
            return
        if name in visiting:
            cycle = " -> ".join([*visiting, name])
            raise ModuleConfigurationError(f"Module dependency cycle: {cycle}")
        try:
            module = registry[name]
        except KeyError as error:
            raise ModuleConfigurationError(f"Unknown or missing module: {name}") from error
        visiting.append(name)
        for dependency in module.requires:
            visit(dependency)
        visiting.pop()
        completed.add(name)
        resolved.append(module)

    for module_name in requested:
        visit(module_name)
    return tuple(resolved)


def validate_module_registrations(modules: tuple[ModuleSpec, ...]) -> None:
    registrations: dict[str, str] = {}
    for module in modules:
        entries = [
            *(f"permission:{item}" for item in module.permissions),
            *(f"job:{item.name}@{item.version}" for item in module.job_handlers),
            *(f"migration:{item.name}" for item in module.migrations),
            *(f"compose:{item.name}" for item in module.compose_capabilities),
        ]
        if module.settings_factory is not None:
            entries.append(f"settings:{module.name}")
        for entry in entries:
            owner = registrations.setdefault(entry, module.name)
            if owner != module.name:
                raise ModuleConfigurationError(
                    f"Duplicate module registration {entry!r}: {owner!r} and {module.name!r}"
                )


def load_profile_modules(settings: Settings) -> tuple[ModuleSpec, ...]:
    requested = PROFILE_MODULES[settings.app_profile]
    registry: dict[str, ModuleSpec] = {}

    from backend_foundation.modules.database.module import module as database_module

    registry[database_module.name] = database_module
    if "temporal" in requested:
        try:
            from backend_foundation.modules.temporal.module import module as temporal_module
        except ModuleNotFoundError as error:
            if error.name and error.name.startswith("temporalio"):
                raise ModuleConfigurationError(
                    "The workflow profile requires the 'workflow' extra; "
                    "install with `uv sync --extra workflow`."
                ) from error
            raise
        registry[temporal_module.name] = temporal_module
    if "jobs" in requested:
        from backend_foundation.modules.jobs.module import module as jobs_module

        registry[jobs_module.name] = jobs_module
    if "users" in requested:
        from backend_foundation.modules.users.module import module as users_module

        registry[users_module.name] = users_module
    if "rbac" in requested:
        from backend_foundation.modules.rbac.module import module as rbac_module

        registry[rbac_module.name] = rbac_module
    if "email" in requested:
        from backend_foundation.modules.email.module import module as email_module

        registry[email_module.name] = email_module
    if "auth" in requested:
        from backend_foundation.modules.auth.module import module as auth_module

        registry[auth_module.name] = auth_module
    if "storage" in requested:
        from backend_foundation.modules.storage.module import module as storage_module

        registry[storage_module.name] = storage_module
    if "ai" in requested:
        try:
            from backend_foundation.modules.ai.module import module as ai_module
        except ModuleNotFoundError as error:
            if error.name and (
                error.name.startswith("langgraph") or error.name.startswith("langchain")
            ):
                raise ModuleConfigurationError(
                    "The ai profile requires the 'ai' extra; install with `uv sync --extra ai`."
                ) from error
            raise
        registry[ai_module.name] = ai_module
    modules = resolve_modules(requested, registry)
    validate_module_registrations(modules)
    return modules
