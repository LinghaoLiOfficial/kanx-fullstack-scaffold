from __future__ import annotations

from pathlib import Path

import boto3  # type: ignore[import-untyped]
import httpx
import pytest
from temporalio.client import Client

from .support import (
    BACKEND,
    IntegrationProject,
    isolated_environment,
    run,
    wait_http,
    write_env_file,
)

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


@pytest.mark.asyncio
async def test_two_generated_full_projects_are_isolated(tmp_path: Path) -> None:
    projects: list[IntegrationProject] = []
    try:
        for suffix in ("alpha", "beta"):
            root = tmp_path / suffix
            run(
                [
                    str(BACKEND),
                    "new",
                    f"integration-{suffix}",
                    "--profile",
                    "full",
                    "--output",
                    str(root),
                ]
            )
            project = None
            for attempt in range(3):
                environment = isolated_environment("full", f"isolation-{suffix}")
                write_env_file(root, environment)
                candidate = IntegrationProject(
                    root,
                    environment,
                    root / "backend" / "compose.yml",
                    package=f"integration_{suffix}",
                    log_root=tmp_path / "logs" / suffix,
                )
                try:
                    candidate.start_infra()
                    candidate.migrate()
                    candidate.start_api()
                    wait_http(
                        f"http://127.0.0.1:{environment['API_PORT']}/health/ready",
                        timeout=120,
                    )
                    project = candidate
                    break
                except AssertionError:
                    candidate.cleanup()
                    if attempt == 2:
                        raise
            assert project is not None
            projects.append(project)

        first, second = projects
        for key in (
            "APP_SLUG",
            "COMPOSE_PROJECT_NAME",
            "DATABASE_NAME",
            "DATABASE_HOST_PORT",
            "TEMPORAL_NAMESPACE",
            "TEMPORAL_TASK_QUEUE",
            "STORAGE_BUCKET",
        ):
            assert first.env[key] != second.env[key]

        for project in projects:
            client = await Client.connect(
                project.env["TEMPORAL_HOST"], namespace=project.env["TEMPORAL_NAMESPACE"]
            )
            assert client.namespace == project.env["TEMPORAL_NAMESPACE"]
            s3 = boto3.client(
                "s3",
                endpoint_url=project.env["STORAGE_ENDPOINT_URL"],
                aws_access_key_id=project.env["STORAGE_ACCESS_KEY"],
                aws_secret_access_key=project.env["STORAGE_SECRET_KEY"],
            )
            buckets = {item["Name"] for item in s3.list_buckets()["Buckets"]}
            assert project.env["STORAGE_BUCKET"] in buckets
            volume = run(
                [
                    "docker",
                    "volume",
                    "inspect",
                    f"{project.env['APP_SLUG']}-postgres-data",
                    "--format",
                    "{{.Name}}",
                ]
            ).stdout.strip()
            assert volume == f"{project.env['APP_SLUG']}-postgres-data"

        first.stop_processes()
        first.compose("stop")
        assert (
            httpx.get(
                f"http://127.0.0.1:{second.env['API_PORT']}/health/ready", timeout=5
            ).status_code
            == 200
        )
        second.processes[0].assert_running()
    finally:
        for project in reversed(projects):
            project.cleanup()
