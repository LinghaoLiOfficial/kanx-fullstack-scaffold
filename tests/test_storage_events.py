import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from backend_foundation.modules.storage.event_consumer import run_event_consumer
from backend_foundation.modules.storage.events import SQSEventSource


@pytest.mark.asyncio
async def test_sqs_event_source_receives_sns_wrapped_and_acknowledges() -> None:
    client = Mock()
    payload = {"Records": [{"eventName": "ObjectCreated"}]}
    client.receive_message.return_value = {
        "Messages": [
            {
                "MessageId": "message",
                "ReceiptHandle": "receipt",
                "Body": json.dumps({"Message": json.dumps(payload)}),
            }
        ]
    }
    source = SQSEventSource(client, "https://queue.example")
    messages = await source.receive(maximum=99, wait_seconds=99)
    assert messages[0]["payload"] == payload
    await source.acknowledge("receipt")
    client.receive_message.assert_called_once_with(
        QueueUrl="https://queue.example", MaxNumberOfMessages=10, WaitTimeSeconds=20
    )
    client.delete_message.assert_called_once_with(
        QueueUrl="https://queue.example", ReceiptHandle="receipt"
    )


@pytest.mark.asyncio
async def test_event_consumer_ingests_acknowledges_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Mock()
    source.receive = AsyncMock(
        side_effect=[
            [
                {
                    "payload": {
                        "Records": [
                            {
                                "eventName": "ObjectCreated",
                                "s3": {"object": {"key": "org/file"}},
                            }
                        ]
                    },
                    "receipt_handle": "receipt",
                }
            ],
            asyncio.CancelledError,
        ]
    )
    source.acknowledge = AsyncMock()

    class Session:
        committed = False

        async def commit(self) -> None:
            self.committed = True

    session = Session()

    class Factory:
        @asynccontextmanager
        async def __call__(self):
            yield session

    engine = Mock(dispose=AsyncMock())
    ingest = AsyncMock()
    monkeypatch.setattr(
        "backend_foundation.modules.storage.event_consumer.Settings",
        lambda: Mock(database_url="postgresql+asyncpg://unused"),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.event_consumer.get_storage_settings",
        lambda: Mock(
            event_queue_url="queue",
            endpoint_url="http://s3",
            access_key=Mock(get_secret_value=Mock(return_value="access")),
            secret_key=Mock(get_secret_value=Mock(return_value="secret")),
            region="test",
        ),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.event_consumer.SQSEventSource", lambda *_a: source
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.event_consumer.create_async_engine",
        lambda *_a, **_k: engine,
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.event_consumer.async_sessionmaker",
        lambda *_a, **_k: Factory(),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.event_consumer.StorageService",
        lambda: Mock(ingest_event=ingest),
    )
    monkeypatch.setattr("backend_foundation.modules.storage.event_consumer.boto3.client", Mock())

    with pytest.raises(asyncio.CancelledError):
        await run_event_consumer()
    assert session.committed
    ingest.assert_awaited_once()
    source.acknowledge.assert_awaited_once_with("receipt")
    engine.dispose.assert_awaited_once()
