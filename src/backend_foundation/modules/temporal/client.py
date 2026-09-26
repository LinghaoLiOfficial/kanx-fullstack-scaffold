from temporalio.client import Client

from backend_foundation.core.config import Settings


async def connect_temporal(settings: Settings, *, lazy: bool = False) -> Client:
    return await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
        lazy=lazy,
    )
