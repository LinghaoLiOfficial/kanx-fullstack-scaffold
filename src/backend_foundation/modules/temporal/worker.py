import asyncio
from collections.abc import Sequence

from temporalio.worker import Worker

from backend_foundation.core.config import AppProfile, Settings, get_settings
from backend_foundation.core.logging import configure_logging
from backend_foundation.core.modules import ModuleSpec, load_profile_modules
from backend_foundation.core.observability import configure_tracing
from backend_foundation.modules.database.session import create_engine, create_session_factory
from backend_foundation.modules.jobs.worker import (
    build_job_activity,
    build_schedule_activity,
    sync_job_types,
)
from backend_foundation.modules.temporal.client import connect_temporal


async def run_worker(
    settings: Settings | None = None,
    modules: Sequence[ModuleSpec] | None = None,
) -> None:
    configured = settings or get_settings()
    if configured.app_profile not in (
        AppProfile.WORKFLOW,
        AppProfile.IDENTITY,
        AppProfile.SAAS,
        AppProfile.FULL,
    ):
        raise RuntimeError("Temporal worker requires a profile with the temporal module")
    configured_modules = tuple(modules) if modules is not None else load_profile_modules(configured)
    workflows = [item for module in configured_modules for item in module.workflows]
    activities = [item for module in configured_modules for item in module.activities]
    handlers = tuple(item for module in configured_modules for item in module.job_handlers)
    engine = create_engine(configured)
    if any(module.name == "jobs" for module in configured_modules):
        factory = create_session_factory(engine)
        await sync_job_types(factory, handlers)
        activities.append(build_job_activity(factory, handlers))
        activities.append(build_schedule_activity(factory))
    if not workflows:
        raise RuntimeError("No Temporal workflows are registered")
    configure_logging(configured)
    configure_tracing(configured)
    client = await connect_temporal(configured)
    try:
        async with Worker(
            client,
            task_queue=configured.temporal_task_queue,
            workflows=workflows,
            activities=activities,
            max_concurrent_activities=configured.temporal_max_concurrent_activities,
        ):
            await asyncio.Event().wait()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass
