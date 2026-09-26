from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from .support import (
    BACKEND,
    database_tables,
    isolated_environment,
    project_resource,
    run,
    write_env_file,
)

pytestmark = [pytest.mark.integration, pytest.mark.profiles]

EXPECTED_TABLES = {
    "api": {"alembic_version"},
    "workflow": {"jobs", "job_attempts", "outbox_messages", "job_schedules"},
    "ai": {"alembic_version"},
    "identity": {
        "users",
        "organizations",
        "organization_memberships",
        "roles",
        "role_permissions",
        "membership_roles",
        "auth_sessions",
        "auth_tokens",
    },
    "saas": {"stored_files"},
    "full": {"stored_files"},
}


@pytest.mark.parametrize("profile", ["api", "workflow", "ai", "identity", "saas", "full"])
async def test_generated_profile_migrates_real_postgres(tmp_path: Path, profile: str) -> None:
    destination = tmp_path / f"generated-{profile}"
    run(
        [
            str(BACKEND),
            "new",
            f"generated-{profile}",
            "--profile",
            profile,
            "--output",
            str(destination),
        ]
    )
    environment = isolated_environment(profile, f"profile-{profile}")
    write_env_file(destination, environment)
    package = f"generated_{profile}"
    with project_resource(destination, environment, package=package) as project:
        project.compose("up", "-d", "--wait", "postgres")
        project.migrate()
        generated_alembic = project.backend_root / ".venv" / "bin" / "alembic"
        alembic_command = (
            [str(generated_alembic), "heads"]
            if generated_alembic.exists()
            else [str(BACKEND.parent / "alembic"), "heads"]
        )
        heads = run(
            alembic_command,
            cwd=project.backend_root,
            env={**environment, "PYTHONPATH": str(project.backend_root / "src")},
        ).stdout.splitlines()
        assert len([line for line in heads if line.strip()]) == 1
        tables = await database_tables(environment["DATABASE_URL"])
        assert EXPECTED_TABLES[profile] <= tables
        doctor = run([str(BACKEND), "doctor", "--project", str(destination)], env=environment)
        assert "ERROR" not in doctor.stdout
        assert shutil.which("docker") is not None
