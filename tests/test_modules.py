import pytest

from backend_foundation.core.config import AppProfile, Settings
from backend_foundation.core.modules import (
    PROFILE_MODULES,
    JobHandlerSpec,
    JobTypeSpec,
    ModuleConfigurationError,
    ModuleSpec,
    load_profile_modules,
    resolve_modules,
    validate_module_registrations,
)


def test_profiles_have_expected_modules() -> None:
    assert PROFILE_MODULES == {
        AppProfile.API: ("database",),
        AppProfile.WORKFLOW: ("database", "temporal", "jobs"),
        AppProfile.AI: ("database", "ai"),
        AppProfile.IDENTITY: ("database", "temporal", "jobs", "users", "rbac", "email", "auth"),
        AppProfile.SAAS: (
            "database",
            "temporal",
            "jobs",
            "users",
            "rbac",
            "email",
            "auth",
            "storage",
        ),
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
    assert [module.name for module in load_profile_modules(Settings(_env_file=None))] == [
        "database"
    ]


def test_resolve_modules_orders_dependencies_once() -> None:
    registry = {
        "base": ModuleSpec("base"),
        "one": ModuleSpec("one", requires=("base",)),
        "two": ModuleSpec("two", requires=("base",)),
    }
    assert [module.name for module in resolve_modules(("one", "two"), registry)] == [
        "base",
        "one",
        "two",
    ]


@pytest.mark.parametrize(
    ("requested", "registry", "message"),
    [
        (("missing",), {}, "Unknown or missing module"),
        (
            ("one",),
            {"one": ModuleSpec("one", requires=("two",))},
            "Unknown or missing module",
        ),
        (
            ("one",),
            {
                "one": ModuleSpec("one", requires=("two",)),
                "two": ModuleSpec("two", requires=("one",)),
            },
            "Module dependency cycle",
        ),
    ],
)
def test_resolve_modules_rejects_invalid_graph(
    requested: tuple[str, ...], registry: dict[str, ModuleSpec], message: str
) -> None:
    with pytest.raises(ModuleConfigurationError, match=message):
        resolve_modules(requested, registry)


def test_duplicate_runtime_registrations_are_rejected() -> None:
    async def handler(_job_id: str, _payload: dict[str, object]) -> None:
        return None

    modules = (
        ModuleSpec("one", job_handlers=(JobHandlerSpec("shared", handler),)),
        ModuleSpec("two", job_handlers=(JobHandlerSpec("shared", handler),)),
    )
    with pytest.raises(ModuleConfigurationError, match="Duplicate module registration"):
        validate_module_registrations(modules)


def test_job_type_versions_can_coexist() -> None:
    async def handler(_job_id: str, _payload: dict[str, object]) -> None:
        return None

    module = ModuleSpec(
        "versioned",
        job_handlers=(
            JobTypeSpec("example", handler, version="1.0.0"),
            JobTypeSpec("example", handler, version="2.0.0", maximum_attempts=2),
        ),
    )
    validate_module_registrations((module,))
