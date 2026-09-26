from __future__ import annotations

import asyncio

import boto3  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ...core.config import Settings
from .events import SQSEventSource
from .router import _normalized_events
from .service import StorageService
from .settings import get_storage_settings


async def run_event_consumer() -> None:
    core = Settings()
    configured = get_storage_settings()
    if not configured.event_queue_url:
        raise RuntimeError("STORAGE_EVENT_QUEUE_URL is required")
    client = boto3.client(
        "sqs",
        endpoint_url=configured.endpoint_url,
        aws_access_key_id=configured.access_key.get_secret_value(),
        aws_secret_access_key=configured.secret_key.get_secret_value(),
        region_name=configured.region,
    )
    source = SQSEventSource(client, configured.event_queue_url)
    engine = create_async_engine(core.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        while True:
            messages = await source.receive()
            for message in messages:
                async with factory() as session:
                    for event in _normalized_events(dict(message["payload"])):
                        await StorageService().ingest_event(session, source="sqs", event=event)
                    await session.commit()
                await source.acknowledge(str(message["receipt_handle"]))
            if not messages:
                await asyncio.sleep(1)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(run_event_consumer())
    except KeyboardInterrupt:
        pass
