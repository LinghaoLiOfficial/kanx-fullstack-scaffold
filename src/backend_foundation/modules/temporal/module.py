from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from temporalio.client import Client

from backend_foundation.core.config import Settings
from backend_foundation.core.health import Check
from backend_foundation.core.modules import ComposeCapability, ModuleSpec
from backend_foundation.modules.temporal.client import connect_temporal
from backend_foundation.modules.temporal.workflows import SmokeWorkflow, database_smoke_activity


@asynccontextmanager
async def temporal_lifespan(app: FastAPI, settings: Settings) -> AsyncIterator[None]:
    app.state.temporal = await connect_temporal(settings, lazy=True)
    yield
    app.state.temporal = None


def temporal_health_checks(app: FastAPI) -> Mapping[str, Check]:
    async def check() -> None:
        temporal: Client = app.state.temporal
        await temporal.service_client.check_health()

    return {"temporal": check}


module = ModuleSpec(
    name="temporal",
    requires=("database",),
    lifespan=temporal_lifespan,
    health_checks=temporal_health_checks,
    workflows=(SmokeWorkflow,),
    activities=(database_smoke_activity,),
    optional_dependencies=("temporalio>=1.18,<2",),
    compose_capabilities=(
        ComposeCapability("temporal"),
        ComposeCapability("temporal-ui"),
        ComposeCapability("namespace"),
    ),
)
