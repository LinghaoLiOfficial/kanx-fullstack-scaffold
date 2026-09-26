from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "bin" / "python"
ALEMBIC = ROOT / ".venv" / "bin" / "alembic"
UVICORN = ROOT / ".venv" / "bin" / "uvicorn"
BACKEND = ROOT / ".venv" / "bin" / "backend"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 60,
    interval: float = 0.25,
    description: str,
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, httpx.HTTPError, KeyError, ValueError) as error:
            last_error = error
        time.sleep(interval)
    detail = f": {last_error}" if last_error else ""
    raise AssertionError(f"Timed out waiting for {description}{detail}")


def wait_http(url: str, *, timeout: float = 90, expected: int = 200) -> httpx.Response:
    def ready() -> httpx.Response | None:
        response = httpx.get(url, timeout=2)
        return response if response.status_code == expected else None

    return wait_until(ready, timeout=timeout, description=url)


def unique_slug(prefix: str) -> str:
    return f"bf-it-{prefix}-{uuid.uuid4().hex[:8]}"


def isolated_environment(profile: str, prefix: str) -> dict[str, str]:
    slug = unique_slug(prefix)
    database_port = free_port()
    temporal_port = free_port()
    temporal_ui_port = free_port()
    smtp_port = free_port()
    mail_ui_port = free_port()
    storage_port = free_port()
    storage_console_port = free_port()
    api_port = free_port()
    gradio_port = free_port()
    database_name = slug.replace("-", "_")
    return {
        **os.environ,
        "APP_NAME": slug,
        "APP_SLUG": slug,
        "APP_PROFILE": profile,
        "APP_ENV": "development",
        "COMPOSE_PROJECT_NAME": slug,
        "DATABASE_USER": "foundation",
        "DATABASE_PASSWORD": "foundation",
        "DATABASE_NAME": database_name,
        "DATABASE_HOST_PORT": str(database_port),
        "DATABASE_URL": (
            f"postgresql+asyncpg://foundation:foundation@127.0.0.1:{database_port}/{database_name}"
        ),
        "TEMPORAL_HOST": f"127.0.0.1:{temporal_port}",
        "TEMPORAL_HOST_PORT": str(temporal_port),
        "TEMPORAL_UI_PORT": str(temporal_ui_port),
        "TEMPORAL_UI_URL": f"http://127.0.0.1:{temporal_ui_port}",
        "TEMPORAL_NAMESPACE": slug,
        "TEMPORAL_TASK_QUEUE": f"{slug}-worker",
        "TEMPORAL_AUTO_REGISTER_NAMESPACE": "true",
        "EMAIL_SMTP_HOST": "127.0.0.1",
        "EMAIL_SMTP_PORT": str(smtp_port),
        "EMAIL_SMTP_HOST_PORT": str(smtp_port),
        "EMAIL_UI_PORT": str(mail_ui_port),
        "EMAIL_PUBLIC_BASE_URL": "http://localhost:3000",
        "STORAGE_ENDPOINT_URL": f"http://127.0.0.1:{storage_port}",
        "STORAGE_ACCESS_KEY": "foundation",
        "STORAGE_SECRET_KEY": "foundation-integration-secret",
        "STORAGE_BUCKET": slug,
        "STORAGE_HOST_PORT": str(storage_port),
        "STORAGE_CONSOLE_PORT": str(storage_console_port),
        "STORAGE_AUTO_CREATE_BUCKET": "true",
        "STORAGE_MULTIPART_THRESHOLD": str(5 * 1024 * 1024),
        "STORAGE_MULTIPART_PART_SIZE": str(5 * 1024 * 1024),
        "STORAGE_PRESIGN_SECONDS": "1",
        "STORAGE_EVENT_WEBHOOK_SECRET": "integration-storage-event-secret",
        "AUTH_JWT_SECRET": "integration-only-jwt-secret-at-least-32-bytes",
        "AUTH_ALLOWED_ORIGINS": "http://localhost:3000",
        "AUTH_SECURE_COOKIES": "false",
        "JOBS_POLL_INTERVAL_SECONDS": "0.1",
        "API_HOST": "127.0.0.1",
        "API_PORT": str(api_port),
        "GRADIO_HOST": "127.0.0.1",
        "GRADIO_PORT": str(gradio_port),
        "GRADIO_ANALYTICS_ENABLED": "false",
        "PYTHONUNBUFFERED": "1",
    }


def write_env_file(project: Path, environment: Mapping[str, str]) -> None:
    keys = (
        "APP_NAME",
        "APP_SLUG",
        "APP_PROFILE",
        "APP_ENV",
        "COMPOSE_PROJECT_NAME",
        "DATABASE_URL",
        "DATABASE_USER",
        "DATABASE_PASSWORD",
        "DATABASE_NAME",
        "DATABASE_HOST_PORT",
        "TEMPORAL_HOST",
        "TEMPORAL_HOST_PORT",
        "TEMPORAL_UI_PORT",
        "TEMPORAL_UI_URL",
        "TEMPORAL_NAMESPACE",
        "TEMPORAL_TASK_QUEUE",
        "TEMPORAL_AUTO_REGISTER_NAMESPACE",
        "EMAIL_SMTP_HOST",
        "EMAIL_SMTP_PORT",
        "EMAIL_SMTP_HOST_PORT",
        "EMAIL_UI_PORT",
        "EMAIL_PUBLIC_BASE_URL",
        "STORAGE_ENDPOINT_URL",
        "STORAGE_ACCESS_KEY",
        "STORAGE_SECRET_KEY",
        "STORAGE_BUCKET",
        "STORAGE_HOST_PORT",
        "STORAGE_CONSOLE_PORT",
        "STORAGE_AUTO_CREATE_BUCKET",
        "STORAGE_MULTIPART_THRESHOLD",
        "STORAGE_MULTIPART_PART_SIZE",
        "STORAGE_PRESIGN_SECONDS",
        "STORAGE_EVENT_WEBHOOK_SECRET",
        "AUTH_JWT_SECRET",
        "AUTH_ALLOWED_ORIGINS",
        "AUTH_SECURE_COOKIES",
        "API_HOST",
        "API_PORT",
        "GRADIO_HOST",
        "GRADIO_PORT",
    )
    (project / ".env").write_text(
        "".join(f"{key}={environment[key]}\n" for key in keys), encoding="utf-8"
    )


async def database_tables(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return set(rows)
    finally:
        await engine.dispose()


def run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    env: Mapping[str, str] | None = None,
    timeout: float = 300,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as error:
        output = "\n".join(part for part in (error.stdout, error.stderr) if part)
        raise AssertionError(
            f"Command failed ({error.returncode}): {' '.join(command)}\n{output}"
        ) from error


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[str]
    log_path: Path
    stream: Any

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        self.stream.close()

    def assert_running(self) -> None:
        if self.process.poll() is not None:
            tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
            raise AssertionError(f"{self.name} exited with {self.process.returncode}:\n{tail}")


@dataclass
class IntegrationProject:
    root: Path
    env: dict[str, str]
    compose_file: Path
    package: str = "backend_foundation"
    log_root: Path | None = None
    processes: list[ManagedProcess] = field(default_factory=list)

    @property
    def backend_root(self) -> Path:
        """Return the runnable backend directory for a legacy or full-stack project."""
        candidate = self.root / "backend"
        return candidate if candidate.is_dir() else self.root

    @property
    def slug(self) -> str:
        return self.env["APP_SLUG"]

    def compose_command(self, *args: str) -> list[str]:
        command = [
            "docker",
            "compose",
            "-p",
            self.env["COMPOSE_PROJECT_NAME"],
            "-f",
            str(self.compose_file),
        ]
        profile = self.env["APP_PROFILE"]
        if profile in {"workflow", "identity", "saas", "full"}:
            command.extend(["--profile", "workflow"])
        if profile in {"identity", "saas", "full"}:
            command.extend(["--profile", "identity"])
        if profile in {"saas", "full"}:
            command.extend(["--profile", "saas"])
        return [*command, *args]

    def compose(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
        return run(
            self.compose_command(*args), cwd=self.backend_root, env=self.env, timeout=timeout
        )

    def start_infra(self) -> None:
        services = ["postgres"]
        profile = self.env["APP_PROFILE"]
        if profile in {"workflow", "identity", "saas", "full"}:
            services.extend(["temporal", "temporal-ui"])
        if profile in {"identity", "saas", "full"}:
            services.append("mailpit")
        if profile in {"saas", "full"}:
            services.append("minio")
        self.compose("up", "-d", "--wait", *services, timeout=360)
        if "temporal" in services:
            self.compose("run", "--rm", "namespace")
        if "minio" in services:
            self.compose("run", "--rm", "minio-init")

    def migrate(self) -> None:
        environment = {**self.env, "PYTHONPATH": str(self.backend_root / "src")}
        run([str(ALEMBIC), "upgrade", "head"], cwd=self.backend_root, env=environment)

    def start(self, name: str, command: list[str]) -> ManagedProcess:
        log_dir = self.log_root or self.root / ".integration-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{name}.log"
        stream = path.open("w", encoding="utf-8")
        environment = {**self.env, "PYTHONPATH": str(self.backend_root / "src")}
        process = subprocess.Popen(
            command,
            cwd=self.backend_root,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
        managed = ManagedProcess(name, process, path, stream)
        self.processes.append(managed)
        return managed

    def start_api(self) -> ManagedProcess:
        return self.start(
            "api",
            [
                str(UVICORN),
                f"{self.package}.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                self.env["API_PORT"],
            ],
        )

    def start_worker(self) -> ManagedProcess:
        return self.start("worker", [str(PYTHON), "-m", f"{self.package}.modules.temporal.worker"])

    def start_dispatcher(self) -> ManagedProcess:
        return self.start(
            "dispatcher", [str(PYTHON), "-m", f"{self.package}.modules.jobs.dispatcher"]
        )

    def start_gradio(self) -> ManagedProcess:
        return self.start("gradio", [str(PYTHON), "-m", f"{self.package}.gradio_app"])

    def stop_processes(self) -> None:
        for process in reversed(self.processes):
            process.stop()
        self.processes.clear()

    def cleanup(self) -> None:
        self.stop_processes()
        try:
            self.compose("down", "--volumes", "--remove-orphans", timeout=180)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            print(f"cleanup warning for {self.slug}: {error}")

    def diagnostics(self) -> None:
        print(f"integration diagnostics for {self.slug}")
        try:
            status = self.compose("ps", "-a")
            print(status.stdout or status.stderr)
        except (AssertionError, subprocess.TimeoutExpired) as error:
            print(f"unable to read compose status: {error}")
        for process in self.processes:
            print(f"{process.name} log: {process.log_path}")
            if process.log_path.exists():
                print(process.log_path.read_text(encoding="utf-8", errors="replace")[-8000:])


@contextmanager
def project_resource(
    root: Path,
    env: dict[str, str],
    *,
    package: str = "backend_foundation",
    log_root: Path | None = None,
) -> Iterator[IntegrationProject]:
    backend_compose = root / "backend" / "compose.yml"
    compose_file = backend_compose if backend_compose.exists() else root / "compose.yml"
    project = IntegrationProject(root, env, compose_file, package, log_root=log_root)
    print(f"integration project: {project.slug}; logs: {project.log_root or project.root}")
    try:
        yield project
    except BaseException:
        project.diagnostics()
        raise
    finally:
        project.cleanup()
