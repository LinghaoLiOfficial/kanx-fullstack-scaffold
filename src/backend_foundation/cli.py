from __future__ import annotations

import argparse
import ast
import asyncio
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from backend_foundation.manifest import (
    ManifestError,
    load_catalog,
    load_profiles,
    load_project_manifest,
    resolve_manifest_modules,
    template_root,
)


class CliError(RuntimeError):
    """A user-facing CLI error."""


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    if not slug:
        raise CliError("Project name must contain letters or digits")
    return slug


def package_name(slug: str) -> str:
    return slug.replace("-", "_")


def _context(
    name: str, profile: str, modules: tuple[str, ...] | None = None
) -> tuple[dict[str, str], tuple[str, ...]]:
    slug = slugify(name)
    package = package_name(slug)
    profiles = load_profiles()
    if modules is None and profile not in profiles:
        raise CliError(f"Unknown profile: {profile}")
    catalog = load_catalog()
    if modules is None:
        modules = resolve_manifest_modules(profiles[profile].modules, catalog)
    else:
        modules = resolve_manifest_modules(modules, catalog)
        if "database" not in modules:
            raise CliError("The database module is required")
    optional: dict[str, str] = {
        "workflow": 'workflow = ["cryptography>=45,<47", "temporalio>=1.18,<2"]',
        "ai": (
            'ai = ["langchain-core>=1.6,<2", "langchain-openai>=1.0,<2", "langgraph>=1.2,<2", '
            '"gradio>=5.49,<7", "langgraph-cli[inmem]>=0.4,<1"]'
        ),
        "identity": (
            'identity = ["aiosmtplib>=4,<6", "cryptography>=45,<47", "email-validator>=2.2,<3", '
            '"pwdlib[argon2]>=0.3,<1", "pyjwt>=2.10,<3", "temporalio>=1.18,<2"]'
        ),
        "full": (
            'full = ["aiosmtplib>=4,<6", "boto3>=1.40,<2", "cryptography>=45,<47", '
            '"email-validator>=2.2,<3", "httpx>=0.28,<1", "pillow>=11,<13", '
            '"langchain-core>=1.6,<2", "langchain-openai>=1.0,<2", "langgraph>=1.2,<2", '
            '"gradio>=5.49,<7", "langgraph-cli[inmem]>=0.4,<1", '
            '"opentelemetry-api>=1.29,<2", "opentelemetry-sdk>=1.29,<2", '
            '"opentelemetry-exporter-otlp-proto-http>=1.29,<2", '
            '"pwdlib[argon2]>=0.3,<1", "pyjwt>=2.10,<3", "temporalio>=1.18,<2"]'
        ),
        "saas": (
            'saas = ["aiosmtplib>=4,<6", "boto3>=1.40,<2", "cryptography>=45,<47", '
            '"email-validator>=2.2,<3", "httpx>=0.28,<1", "pillow>=11,<13", '
            '"pwdlib[argon2]>=0.3,<1", "pyjwt>=2.10,<3", "temporalio>=1.18,<2"]'
        ),
    }
    enabled_extras: tuple[str, ...] = ("workflow",) if "temporal" in modules else ()
    if "ai" in modules:
        enabled_extras += ("ai",)
    if "auth" in modules or "email" in modules:
        enabled_extras += ("identity",)
    if "storage" in modules:
        enabled_extras += ("saas",)
    if "ai" in modules and "storage" in modules:
        enabled_extras += ("full",)
    extras = "\n".join(optional[name] for name in enabled_extras)
    sync_extras = " ".join(f"--extra {name}" for name in enabled_extras)
    context = {
        "PROJECT_NAME": name,
        "PROJECT_SLUG": slug,
        "PACKAGE_NAME": package,
        "DATABASE_NAME": package,
        "TEMPORAL_NAMESPACE": slug,
        "TEMPORAL_TASK_QUEUE": f"{slug}-worker",
        "PROFILE": profile,
        "MODULES": repr(modules),
        "OPTIONAL_DEPENDENCIES": extras or "# No optional runtime extras for this profile.",
        "SYNC_EXTRAS": sync_extras,
    }
    return context, modules


def _render(value: str, context: dict[str, str]) -> str:
    for key, replacement in context.items():
        value = value.replace("{{" + key + "}}", replacement)
    return value


def _write_template(source: Path, destination: Path, context: dict[str, str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise CliError(f"Refusing to overwrite existing file: {destination}")
    destination.write_text(_render(source.read_text(encoding="utf-8"), context), encoding="utf-8")


def _copy_tree(source: Path, destination: Path, context: dict[str, str]) -> None:
    for path in source.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            relative = Path(_render(str(path.relative_to(source)), context))
            _write_template(path, destination / relative, context)


def _module_files(module: str, context: dict[str, str]) -> list[tuple[Path, Path]]:
    source = template_root() / "backend" / "modules" / module / "files"
    return [
        (path, Path(_render(str(path.relative_to(source)), context)))
        for path in source.rglob("*")
        if path.is_file() and path.name != "installed_modules.py"
    ]


def _installed_modules_content(modules: tuple[str, ...]) -> str:
    return (
        "INSTALLED_MODULES = (\n"
        + "".join(f"    {json.dumps(item)},\n" for item in modules)
        + ")\n"
    )


def _write_installed_modules(project: Path, package: str, modules: tuple[str, ...]) -> None:
    path = project / "src" / package / "core" / "installed_modules.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_installed_modules_content(modules), encoding="utf-8")


def _installed_models_content(modules: tuple[str, ...]) -> str:
    catalog = load_catalog()
    imports = [item for module in modules for item in catalog[module].model_imports]
    lines = ["# Generated by backend; explicit imports register SQLAlchemy metadata."]
    for index, import_path in enumerate(sorted(imports)):
        parent, name = import_path.rsplit(".", 1)
        relative = "." * 2 + parent
        lines.append(f"from {relative} import {name} as _models_{index}  # noqa: F401")
    lines.extend(["", "REGISTERED_MODEL_MODULES = ("])
    lines.extend(f"    {json.dumps(item)}," for item in imports)
    lines.extend([")", ""])
    return "\n".join(lines)


def _write_installed_models(project: Path, package: str, modules: tuple[str, ...]) -> None:
    path = project / "src" / package / "core" / "installed_models.py"
    path.write_text(_installed_models_content(modules), encoding="utf-8")


def _write_module_migrations(
    project: Path, modules: tuple[str, ...], context: dict[str, str]
) -> None:
    versions = project / "alembic" / "versions"
    previous = "foundation_0001"
    existing = sorted(versions.glob("foundation_*.py"))
    if existing:
        previous = existing[-1].stem
        sequence = int(previous.split("_")[1]) + 1
    else:
        sequence = 2
    for module in modules:
        manifest = load_catalog()[module]
        for migration_name in manifest.migrations:
            source = template_root() / "backend" / "modules" / module / migration_name
            if not source.is_file():
                raise CliError(f"Module migration is missing: {module}/{migration_name}")
            revision = f"foundation_{sequence:04d}_{module}"
            migration_context = {
                **context,
                "REVISION": revision,
                "DOWN_REVISION": previous,
            }
            target = versions / f"{revision}.py"
            _write_template(source, target, migration_context)
            previous = revision
            sequence += 1


def _write_manifest(path: Path, context: dict[str, str], modules: tuple[str, ...]) -> None:
    path.write_text(
        "\n".join(
            [
                "composition_version = 2",
                f"name = {json.dumps(context['PROJECT_NAME'])}",
                'version = "0.1.0"',
                'description = "Generated backend project"',
                f"slug = {json.dumps(context['PROJECT_SLUG'])}",
                f"package = {json.dumps(context['PACKAGE_NAME'])}",
                f"profile = {json.dumps(context['PROFILE'])}",
                f"modules = {json.dumps(list(modules))}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _create_backend(destination: Path, context: dict[str, str], modules: tuple[str, ...]) -> None:
    _copy_tree(template_root() / "backend" / "base", destination, context)
    pyproject = destination / "pyproject.toml"
    rendered = pyproject.read_text(encoding="utf-8")
    rendered = rendered.replace(
        f"# {context['OPTIONAL_DEPENDENCIES']}", context["OPTIONAL_DEPENDENCIES"]
    )
    rendered = rendered.replace(
        f'# {context["PACKAGE_NAME"]} = "{context["PACKAGE_NAME"]}.app:app"',
        f'{context["PACKAGE_NAME"]} = "{context["PACKAGE_NAME"]}.app:app"',
    )
    pyproject.write_text(rendered, encoding="utf-8")
    for module in modules:
        for source, relative in _module_files(module, context):
            _write_template(source, destination / relative, context)
    _write_installed_modules(destination, context["PACKAGE_NAME"], modules)
    _write_installed_models(destination, context["PACKAGE_NAME"], modules)
    _write_module_migrations(destination, modules, context)
    _write_manifest(destination / "module.toml", context, modules)
    shutil.copyfile(destination / ".env.example", destination / ".env")


def _frontend_capabilities(modules: tuple[str, ...]) -> tuple[str, ...]:
    capabilities = ["core"]
    if "auth" in modules:
        capabilities.extend(["auth", "organizations"])
    if "storage" in modules:
        capabilities.append("storage")
    if "ai" in modules:
        capabilities.append("ai-tools")
    if "temporal" in modules:
        capabilities.append("workflow-tools")
    return tuple(capabilities)


def _create_frontend(destination: Path, context: dict[str, str], modules: tuple[str, ...]) -> None:
    capabilities = _frontend_capabilities(modules)
    frontend_context = {
        **context,
        "FRONTEND_CAPABILITIES": json.dumps(list(capabilities)),
        "AUTH_ENABLED": str("auth" in modules).lower(),
        "STORAGE_ENABLED": str("storage" in modules).lower(),
        "AI_ENABLED": str("ai" in modules).lower(),
        "WORKFLOW_ENABLED": str("temporal" in modules).lower(),
    }
    _copy_tree(template_root() / "frontend" / "base", destination, frontend_context)
    for capability in ("auth", "storage"):
        if capability in capabilities:
            _copy_tree(
                template_root() / "frontend" / "modules" / capability,
                destination,
                frontend_context,
            )
    shutil.copyfile(destination / ".env.example", destination / ".env")
    (destination / "capabilities.json").write_text(
        json.dumps({"profile": context["PROFILE"], "capabilities": capabilities}, indent=2) + "\n",
        encoding="utf-8",
    )


def create_project(
    name: str,
    profile: str,
    output: Path | None,
    modules: tuple[str, ...] | None = None,
) -> Path:
    context, modules = _context(name, profile, modules)
    destination = (output or Path.cwd() / context["PROJECT_SLUG"]).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise CliError(f"Output directory is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    _copy_tree(template_root() / "fullstack" / "base", destination, context)
    _create_backend(destination / "backend", context, modules)
    _create_frontend(destination / "frontend", context, modules)
    print(f"Created {destination}")
    print(f"Next: cd {destination} && make dev")
    return destination


DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "backend-foundation-downloads"
DOWNLOAD_MAX_AGE_SECONDS = 3600
DOWNLOAD_MAX_FILES = 20


def _cleanup_downloads() -> None:
    if not DOWNLOAD_DIR.exists():
        return
    now = time.time()
    archives = sorted(DOWNLOAD_DIR.glob("*.zip"), key=lambda item: item.stat().st_mtime)
    for archive in archives:
        if now - archive.stat().st_mtime > DOWNLOAD_MAX_AGE_SECONDS:
            archive.unlink(missing_ok=True)
    archives = sorted(DOWNLOAD_DIR.glob("*.zip"), key=lambda item: item.stat().st_mtime)
    for archive in archives[:-DOWNLOAD_MAX_FILES]:
        archive.unlink(missing_ok=True)


def create_project_archive(
    name: str,
    profile: str,
    modules: tuple[str, ...] | None = None,
) -> Path:
    """Generate a project into a temporary directory and return a safe ZIP path."""
    _cleanup_downloads()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(name)
    with tempfile.TemporaryDirectory(prefix="backend-foundation-project-") as workspace:
        project = create_project(name, profile, Path(workspace) / slug, modules)
        archive = DOWNLOAD_DIR / f"{slug}-{uuid.uuid4().hex[:8]}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path in project.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(project)
                if any(part in {".git", ".venv", "tmp", "__pycache__"} for part in relative.parts):
                    continue
                if path.name == ".coverage" or path.name.endswith(".pyc"):
                    continue
                output.write(path, Path(slug) / relative)
        _cleanup_downloads()
        return archive


def _env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _doctor_backend(project: Path, environment: str | None = None) -> int:
    project = project.resolve()
    errors = 0
    warnings = 0
    try:
        manifest = load_project_manifest(project / "module.toml")
        if manifest.composition_version != 2:
            raise ManifestError("composition_version must be 2")
        catalog = load_catalog()
        profiles = load_profiles()
        if manifest.profile != "custom" and manifest.profile not in profiles:
            raise ManifestError(f"Unknown profile: {manifest.profile}")
        if slugify(manifest.name) != manifest.slug:
            raise ManifestError("slug does not match project name")
        resolved = resolve_manifest_modules(manifest.modules, catalog)
        if tuple(resolved) != tuple(manifest.modules):
            raise ManifestError("modules are not in dependency order or contain duplicates")
        if manifest.package != package_name(manifest.slug):
            raise ManifestError("package does not match slug")
        expected = (
            resolved
            if manifest.profile == "custom"
            else resolve_manifest_modules(profiles[manifest.profile].modules, catalog)
        )
        if expected != resolved:
            raise ManifestError(f"profile {manifest.profile!r} requires modules {expected!r}")
        installed = project / "src" / manifest.package / "core" / "installed_modules.py"
        if not installed.exists():
            raise ManifestError("core/installed_modules.py is missing")
        installed_source = ast.parse(installed.read_text(encoding="utf-8"))
        installed_value = next(
            (
                ast.literal_eval(node.value)
                for node in installed_source.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "INSTALLED_MODULES"
                    for target in node.targets
                )
            ),
            None,
        )
        if tuple(installed_value or ()) != manifest.modules:
            raise ManifestError("installed_modules.py differs from module.toml")
        model_file = project / "src" / manifest.package / "core" / "installed_models.py"
        if not model_file.exists():
            raise ManifestError("core/installed_models.py is missing")
        for name in resolved:
            if catalog[name].model_imports and not list(
                (project / "alembic" / "versions").glob(f"foundation_*_{name}.py")
            ):
                raise ManifestError(f"Alembic migration for module {name!r} is missing")
        migration_files = sorted((project / "alembic" / "versions").glob("*.py"))
        revisions: dict[str, str | None] = {}
        for migration in migration_files:
            tree = ast.parse(migration.read_text(encoding="utf-8"))
            values = {
                node.target.id: ast.literal_eval(node.value)
                for node in tree.body
                if isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.value is not None
                and node.target.id in {"revision", "down_revision"}
            }
            if "revision" not in values:
                raise ManifestError(f"Migration has no revision: {migration.name}")
            if values["revision"] in revisions:
                raise ManifestError(f"Duplicate Alembic revision: {values['revision']}")
            revisions[str(values["revision"])] = values.get("down_revision")
        heads = set(revisions)
        heads.difference_update(value for value in revisions.values() if value is not None)
        if len(heads) != 1:
            raise ManifestError(f"Expected a linear Alembic history, found heads: {sorted(heads)}")
        print(f"PASS manifest: {manifest.name}")
        print(f"PASS modules: {', '.join(resolved)}")
        print("PASS composition: version 2 module and model registries")
        print("PASS alembic: linear module migration chain")
    except (CliError, ManifestError, OSError) as error:
        print(f"ERROR manifest: {error}")
        return 1

    env_path = project / (f".env.{environment}" if environment else ".env")
    env = _env_file(env_path)
    if not env:
        print(f"WARN {env_path.name}: missing; deployed environments may use platform injection")
        warnings += 1
    else:
        url = env.get("DATABASE_URL", "")
        host_port = env.get("DATABASE_HOST_PORT", "")
        parsed = urlparse(url)
        if host_port and parsed.port and str(parsed.port) != host_port:
            print("ERROR database: DATABASE_URL port differs from DATABASE_HOST_PORT")
            errors += 1
        else:
            print("PASS database: connection settings")
        if (
            env.get("APP_SLUG")
            and env.get("COMPOSE_PROJECT_NAME", env["APP_SLUG"]) != env["APP_SLUG"]
        ):
            print("ERROR compose: project name differs from APP_SLUG")
            errors += 1
        else:
            print("PASS compose: project isolation")
        if env.get("APP_PROFILE") and env["APP_PROFILE"] != manifest.profile:
            print("ERROR config: APP_PROFILE differs from module.toml profile")
            errors += 1
        if "temporal" in manifest.modules:
            for key in ("TEMPORAL_HOST", "TEMPORAL_NAMESPACE", "TEMPORAL_TASK_QUEUE"):
                if not env.get(key):
                    print(f"ERROR temporal: {key} is required")
                    errors += 1
            if not errors:
                print("PASS temporal: connection settings")
        for name in manifest.modules:
            for key in catalog[name].required_env:
                if not env.get(key):
                    print(f"ERROR config: {key} is required by module {name}")
                    errors += 1
        selected_environment = environment or env.get("APP_ENV", "local").lower()
        if selected_environment in {"staging", "production"} and "auth" in manifest.modules:
            secret = env.get("AUTH_JWT_SECRET", "")
            if len(secret) < 32 or secret == "development-only-change-me-32-bytes":
                print("ERROR auth: production AUTH_JWT_SECRET must be a unique secret of 32+ chars")
                errors += 1
            if env.get("AUTH_SECURE_COOKIES", "").lower() != "true":
                print("ERROR auth: deployed environments require secure cookies")
                errors += 1
        if selected_environment in {"staging", "production"} and "storage" in manifest.modules:
            if env.get("STORAGE_AUTO_CREATE_BUCKET", "").lower() != "false":
                print("ERROR storage: deployed environments may not auto-create buckets")
                errors += 1

    if not (project / "alembic.ini").exists() or not (project / "alembic").exists():
        print("ERROR alembic: configuration or directory missing")
        errors += 1
    else:
        print("PASS alembic: configuration")

    pyproject = (
        (project / "pyproject.toml").read_text(encoding="utf-8")
        if (project / "pyproject.toml").exists()
        else ""
    )
    for extra, module in (
        ("workflow", "temporal"),
        ("ai", "ai"),
        ("identity", "auth"),
        ("saas", "storage"),
    ):
        if module in manifest.modules and f"{extra} =" not in pyproject:
            print(f"ERROR dependencies: missing optional extra {extra!r}")
            errors += 1

    if shutil.which("docker") is None:
        print("WARN docker: not installed")
        warnings += 1
    elif subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("WARN docker: engine is not available")
        warnings += 1
    else:
        print("PASS docker: engine available")
        compose_file = project / "compose.yml"
        if compose_file.exists():
            compose_project = env.get("COMPOSE_PROJECT_NAME", manifest.slug)
            command = ["docker", "compose", "-p", compose_project, "-f", str(compose_file)]
            capabilities = {
                item for name in manifest.modules for item in catalog[name].compose_files
            }
            if "mailpit" in capabilities:
                command.extend(["--profile", "identity"])
            if "minio" in capabilities:
                command.extend(["--profile", "saas"])
            if "temporal" in manifest.modules:
                command.extend(["--profile", "workflow"])
            result = subprocess.run(
                [*command, "config"],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                print(f"ERROR compose: invalid configuration ({result.stderr.strip()})")
                errors += 1
            else:
                print("PASS compose: configuration")
                services = subprocess.run(
                    [*command, "config", "--services"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.splitlines()
                missing = sorted(capabilities.difference(services))
                if missing:
                    print(f"ERROR compose: missing module services {', '.join(missing)}")
                    errors += 1

    for key, service in (
        ("DATABASE_HOST_PORT", "postgres"),
        ("TEMPORAL_HOST_PORT", "temporal"),
        ("TEMPORAL_UI_PORT", "temporal-ui"),
    ):
        if service == "temporal" and "temporal" not in manifest.modules:
            continue
        try:
            port = int(env.get(key, "0"))
        except ValueError:
            port = 0
        if port and not _port_is_free(port):
            print(f"WARN ports: {service} port {port} is currently occupied")
            warnings += 1

    if errors:
        return 1
    print(f"doctor: ok ({warnings} warning(s))")
    return 0


def doctor(project: Path, environment: str | None = None) -> int:
    project = project.resolve()
    backend = project / "backend"
    frontend = project / "frontend"
    if not backend.is_dir() or not frontend.is_dir():
        print("ERROR fullstack: expected backend/ and frontend/ directories")
        return 1
    required = (
        project / "README.md",
        project / "Makefile",
        backend / ".env",
        backend / ".env.example",
        backend / "README.md",
        frontend / ".env",
        frontend / ".env.example",
        frontend / "README.md",
        frontend / "package.json",
        frontend / "capabilities.json",
    )
    missing = [str(path.relative_to(project)) for path in required if not path.is_file()]
    if missing:
        print(f"ERROR fullstack: missing files: {', '.join(missing)}")
        return 1

    backend_env = _env_file(backend / ".env")
    frontend_env = _env_file(frontend / ".env")
    app_url = frontend_env.get("NEXT_PUBLIC_APP_URL", "")
    api_url = frontend_env.get("NEXT_PUBLIC_API_BASE_URL", "")
    origins = {item.strip() for item in backend_env.get("CORS_ALLOWED_ORIGINS", "").split(",")}
    auth_origins = {item.strip() for item in backend_env.get("AUTH_ALLOWED_ORIGINS", "").split(",")}
    errors = 0
    if not app_url or app_url not in origins:
        print("ERROR fullstack: frontend URL is not allowed by backend CORS")
        errors += 1
    if "auth" in load_project_manifest(backend / "module.toml").modules and (
        app_url not in auth_origins
    ):
        print("ERROR fullstack: frontend URL is not allowed by auth Origin policy")
        errors += 1
    try:
        if urlparse(api_url).port != int(backend_env.get("API_PORT", "0")):
            raise ValueError
    except (TypeError, ValueError):
        print("ERROR fullstack: frontend API URL differs from backend API_PORT")
        errors += 1
    if errors:
        return 1
    print("PASS fullstack: frontend and backend environment contract")
    return _doctor_backend(backend, environment)


async def _set_platform_admin(email: str, *, grant: bool) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend_foundation.core.config import Settings
    from backend_foundation.modules.jobs.models import PlatformRoleAssignment
    from backend_foundation.modules.users.models import User

    engine = create_async_engine(Settings().database_url, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            user = await session.scalar(select(User).where(User.email == email.strip().lower()))
            if user is None:
                raise CliError(f"User not found: {email}")
            assignment = await session.scalar(
                select(PlatformRoleAssignment).where(
                    PlatformRoleAssignment.user_id == user.id,
                    PlatformRoleAssignment.role == "jobs_admin",
                )
            )
            if grant and assignment is None:
                session.add(PlatformRoleAssignment(user_id=user.id, role="jobs_admin"))
            elif not grant and assignment is not None:
                await session.delete(assignment)
            await session.commit()
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backend")
    subparsers = parser.add_subparsers(dest="command", required=True)
    new_parser = subparsers.add_parser("new")
    new_parser.add_argument("project_name")
    new_parser.add_argument("--profile", choices=tuple(load_profiles()), default="api")
    new_parser.add_argument("--output", type=Path)
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--project", type=Path, default=Path.cwd())
    doctor_parser.add_argument("--env", choices=("local", "staging", "production"))
    platform_parser = subparsers.add_parser("platform-admin")
    platform_parser.add_argument("action", choices=("grant", "revoke"))
    platform_parser.add_argument("email")
    args = parser.parse_args(argv)
    try:
        if args.command == "new":
            create_project(args.project_name, args.profile, args.output)
            return 0
        if args.command == "platform-admin":
            asyncio.run(_set_platform_admin(args.email, grant=args.action == "grant"))
            print(f"Platform jobs admin {args.action}: {args.email}")
            return 0
        return doctor(args.project, args.env)
    except (CliError, ManifestError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
