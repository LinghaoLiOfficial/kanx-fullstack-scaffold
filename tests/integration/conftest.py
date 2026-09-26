from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator

import pytest

from .support import ROOT, IntegrationProject, isolated_environment, project_resource, wait_http


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("BACKEND_RUN_INTEGRATION") == "1":
        return
    reason = "set BACKEND_RUN_INTEGRATION=1 or use make integration"
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def full_infrastructure(tmp_path_factory: pytest.TempPathFactory) -> Iterator[IntegrationProject]:
    subprocess.run(["docker", "info"], check=True, capture_output=True)
    environment = isolated_environment("full", "shared")
    log_root = tmp_path_factory.mktemp("integration-logs")
    with project_resource(ROOT, environment, log_root=log_root) as project:
        project.start_infra()
        project.migrate()
        yield project


@pytest.fixture(scope="session")
def full_runtime(full_infrastructure: IntegrationProject) -> Iterator[IntegrationProject]:
    project = full_infrastructure
    project.start_worker()
    project.start_dispatcher()
    project.start_api()
    project.start_gradio()
    wait_http(f"http://127.0.0.1:{project.env['API_PORT']}/health/ready", timeout=120)
    wait_http(f"http://127.0.0.1:{project.env['GRADIO_PORT']}", timeout=120)
    wait_http(project.env["TEMPORAL_UI_URL"], timeout=120)
    wait_http(f"http://127.0.0.1:{project.env['EMAIL_UI_PORT']}", timeout=120)
    wait_http(f"http://127.0.0.1:{project.env['STORAGE_HOST_PORT']}/minio/health/live")
    for process in project.processes:
        process.assert_running()
    yield project
