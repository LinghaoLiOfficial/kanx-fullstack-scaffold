import argparse
import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from backend_foundation.core.config import AppProfile, Settings, get_settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.yml"


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    output = subprocess.DEVNULL if quiet else None
    return subprocess.run(
        command, check=True, text=True, cwd=cwd, env=env, stdout=output, stderr=output
    )


def _compose_environment(settings: Settings) -> dict[str, str]:
    environment = {
        **os.environ,
        "APP_SLUG": settings.app_slug,
        "DATABASE_USER": settings.database_user,
        "DATABASE_PASSWORD": settings.database_password,
        "DATABASE_NAME": settings.database_name,
        "DATABASE_HOST_PORT": str(settings.database_host_port),
        "TEMPORAL_HOST_PORT": str(settings.temporal_host_port),
        "TEMPORAL_UI_PORT": str(settings.temporal_ui_port),
        "TEMPORAL_NAMESPACE": settings.temporal_namespace,
        "EMAIL_SMTP_HOST_PORT": str(settings.email_smtp_host_port),
        "EMAIL_UI_PORT": str(settings.email_ui_port),
        "STORAGE_HOST_PORT": str(settings.storage_host_port),
        "STORAGE_CONSOLE_PORT": str(settings.storage_console_port),
    }
    if settings.app_profile in (AppProfile.SAAS, AppProfile.FULL):
        from backend_foundation.modules.storage.settings import get_storage_settings

        storage = get_storage_settings()
        environment.update(
            {
                "STORAGE_ACCESS_KEY": storage.access_key.get_secret_value(),
                "STORAGE_SECRET_KEY": storage.secret_key.get_secret_value(),
                "STORAGE_BUCKET": storage.bucket,
            }
        )
    return environment


def _profile_enabled(settings: Settings) -> bool:
    return settings.app_profile in (
        AppProfile.WORKFLOW,
        AppProfile.IDENTITY,
        AppProfile.SAAS,
        AppProfile.FULL,
    )


def _jobs_enabled(settings: Settings) -> bool:
    return _profile_enabled(settings)


def _ai_enabled(settings: Settings) -> bool:
    return settings.app_profile in (AppProfile.AI, AppProfile.FULL)


def _compose(settings: Settings, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "-p", settings.docker_project_name, "-f", str(COMPOSE_FILE)]
    if _profile_enabled(settings):
        command.extend(["--profile", "workflow"])
    if settings.app_profile in (AppProfile.IDENTITY, AppProfile.SAAS, AppProfile.FULL):
        command.extend(["--profile", "identity"])
    if settings.app_profile in (AppProfile.SAAS, AppProfile.FULL):
        command.extend(["--profile", "saas"])
    command.extend(arguments)
    return _run(command, cwd=ROOT, env=_compose_environment(settings))


def check_prerequisites() -> None:
    for command in ("uv", "docker"):
        if shutil.which(command) is None:
            raise RuntimeError(f"Required command is not installed: {command}")
    _run(["docker", "info"], quiet=True)
    _run(["docker", "compose", "version"], quiet=True)


def ensure_port_available(settings: Settings, port: int, service: str) -> None:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return
        except OSError:
            pass
    owners = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"publish={port}",
            "--format",
            '{{.Label "com.docker.compose.project"}}/{{.Label "com.docker.compose.service"}}',
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if f"{settings.docker_project_name}/{service}" in owners:
        return
    raise RuntimeError(
        f"Port {port} for {service} is occupied outside {settings.docker_project_name}"
    )


def wait_for_port(host: str, port: int, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {host}:{port}")


def _display_host(host: str) -> str:
    """Turn bind/wildcard hosts into a useful browser address."""
    if host in {"0.0.0.0", "::", "[::]", "localhost"}:
        return "127.0.0.1"
    return host.split(":", 1)[0] if host.count(":") == 1 else host


def _local_url(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{_display_host(host)}:{port}"


def _temporal_ui_address(settings: Settings) -> str:
    parsed = urlsplit(settings.temporal_ui_url)
    if parsed.hostname in {"localhost", "127.0.0.1"}:
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme or 'http'}://{parsed.hostname}:{settings.temporal_ui_port}{path}"
    return settings.temporal_ui_url.rstrip("/")


def service_urls(settings: Settings, *, include_local: bool = True) -> list[tuple[str, str]]:
    """Return safe, credential-free addresses for services in the active profile."""
    urls: list[tuple[str, str]] = []
    if include_local:
        urls.append(("API", _local_url("http", settings.api_host, settings.api_port)))
        if _ai_enabled(settings):
            urls.append(("Gradio", _local_url("http", settings.gradio_host, settings.gradio_port)))
    urls.append(("PostgreSQL", _local_url("postgresql", "localhost", settings.database_host_port)))
    if _profile_enabled(settings):
        urls.append(("Temporal", _local_url("temporal", "localhost", settings.temporal_host_port)))
        urls.append(("Temporal UI", _temporal_ui_address(settings)))
    if settings.app_profile in (AppProfile.IDENTITY, AppProfile.SAAS, AppProfile.FULL):
        urls.append(
            ("Mailpit SMTP", _local_url("smtp", "localhost", settings.email_smtp_host_port))
        )
        urls.append(("Mailpit UI", _local_url("http", "localhost", settings.email_ui_port)))
    if settings.app_profile in (AppProfile.SAAS, AppProfile.FULL):
        urls.append(("MinIO API", _local_url("http", "localhost", settings.storage_host_port)))
        urls.append(
            ("MinIO Console", _local_url("http", "localhost", settings.storage_console_port))
        )
    return urls


def print_service_urls(settings: Settings, *, include_local: bool = True) -> None:
    print("\nServices started:")
    for label, url in service_urls(settings, include_local=include_local):
        print(f"  {label:<16} {url}")
    print(flush=True)


def infra_up(settings: Settings) -> None:
    check_prerequisites()
    ensure_port_available(settings, settings.database_host_port, "postgres")
    if _profile_enabled(settings):
        ensure_port_available(settings, settings.temporal_host_port, "temporal")
        ensure_port_available(settings, settings.temporal_ui_port, "temporal-ui")
    if settings.app_profile in (AppProfile.IDENTITY, AppProfile.SAAS, AppProfile.FULL):
        ensure_port_available(settings, settings.email_smtp_host_port, "mailpit")
        ensure_port_available(settings, settings.email_ui_port, "mailpit")
    if settings.app_profile in (AppProfile.SAAS, AppProfile.FULL):
        ensure_port_available(settings, settings.storage_host_port, "minio")
        ensure_port_available(settings, settings.storage_console_port, "minio")
    services = ["postgres"]
    if _profile_enabled(settings):
        services.extend(["temporal", "temporal-ui"])
    if settings.app_profile in (AppProfile.IDENTITY, AppProfile.SAAS, AppProfile.FULL):
        services.append("mailpit")
    if settings.app_profile in (AppProfile.SAAS, AppProfile.FULL):
        services.append("minio")
    _compose(settings, "up", "-d", "--wait", *services)
    wait_for_port("127.0.0.1", settings.database_host_port)
    if _profile_enabled(settings):
        wait_for_port("127.0.0.1", settings.temporal_host_port)
        _compose(settings, "run", "--rm", "namespace")
    if settings.app_profile in (AppProfile.IDENTITY, AppProfile.SAAS, AppProfile.FULL):
        wait_for_port("127.0.0.1", settings.email_smtp_host_port)
    if settings.app_profile in (AppProfile.SAAS, AppProfile.FULL):
        wait_for_port("127.0.0.1", settings.storage_host_port)
        _compose(settings, "run", "--rm", "minio-init")


async def initialize_namespace(settings: Settings) -> None:
    if not _profile_enabled(settings) or not settings.auto_register_namespace:
        return
    from google.protobuf.duration_pb2 import Duration
    from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest
    from temporalio.client import Client
    from temporalio.service import RPCError, RPCStatusCode

    client = await Client.connect(settings.temporal_host)
    await client.service_client.check_health()
    try:
        await client.service_client.workflow_service.register_namespace(
            RegisterNamespaceRequest(
                namespace=settings.temporal_namespace,
                description=f"{settings.app_name} local development namespace",
                workflow_execution_retention_period=Duration(seconds=7 * 24 * 60 * 60),
            )
        )
    except RPCError as error:
        if error.status != RPCStatusCode.ALREADY_EXISTS:
            raise


def migrate() -> None:
    try:
        _run(["uv", "run", "alembic", "upgrade", "head"], cwd=ROOT)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "Database migration failed. Check DATABASE_URL and PostgreSQL availability. "
            "If this database used the retired revision chain, create a fresh empty database."
        ) from error


async def smoke(settings: Settings) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()
    results = ["database"]
    if _profile_enabled(settings):
        from temporalio.client import Client

        from backend_foundation.modules.temporal.workflows import SmokeWorkflow

        client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
        result = await client.execute_workflow(
            SmokeWorkflow.run,
            settings.database_url,
            id=f"{settings.app_slug}-smoke-{time.time_ns()}",
            task_queue=settings.temporal_task_queue,
        )
        if result != "ok-workflow":
            raise RuntimeError(f"Unexpected workflow smoke result: {result!r}")
        results.append("workflow")
    if _ai_enabled(settings):
        from backend_foundation.modules.ai.graph import run_smoke_graph

        if await run_smoke_graph("ok") != "ok-graph":
            raise RuntimeError("Unexpected AI smoke result")
        results.append("ai")
    print(f"smoke: ok ({', '.join(results)})")


def run_application_processes(settings: Settings) -> None:
    commands = [
        [
            "uv",
            "run",
            "uvicorn",
            "backend_foundation.app:app",
            "--host",
            settings.api_host,
            "--port",
            str(settings.api_port),
            "--reload",
        ]
    ]
    if _profile_enabled(settings):
        commands.append(["uv", "run", "python", "-m", "backend_foundation.modules.temporal.worker"])
    if _jobs_enabled(settings):
        commands.append(["uv", "run", "python", "-m", "backend_foundation.modules.jobs.dispatcher"])
    if _ai_enabled(settings):
        commands.append(["uv", "run", "python", "-m", "backend_foundation.gradio_app"])
    processes = [subprocess.Popen(command, cwd=ROOT) for command in commands]
    wait_for_port("127.0.0.1", settings.api_port)
    if _ai_enabled(settings):
        wait_for_port("127.0.0.1", settings.gradio_port)
    print_service_urls(settings)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopping and all(process.poll() is None for process in processes):
            time.sleep(0.25)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    if not stopping and any(process.returncode != 0 for process in processes):
        raise RuntimeError("An application process exited unexpectedly; its peers were stopped")


def dev(settings: Settings) -> None:
    infra_up(settings)
    migrate()
    asyncio.run(initialize_namespace(settings))
    run_application_processes(settings)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local development orchestration")
    parser.add_argument(
        "command", choices=("dev", "infra-up", "infra-down", "infra-status", "infra-reset", "smoke")
    )
    command = parser.parse_args().command
    settings = get_settings()
    try:
        if command == "dev":
            dev(settings)
        elif command == "infra-up":
            infra_up(settings)
            print_service_urls(settings, include_local=False)
        elif command == "infra-down":
            check_prerequisites()
            _compose(settings, "down", "--remove-orphans")
        elif command == "infra-status":
            check_prerequisites()
            _compose(settings, "ps")
        elif command == "infra-reset":
            check_prerequisites()
            _compose(settings, "down", "--volumes", "--remove-orphans")
        else:
            asyncio.run(smoke(settings))
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
