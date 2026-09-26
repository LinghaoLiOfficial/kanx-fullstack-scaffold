from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

from botocore.client import BaseClient  # type: ignore[import-untyped]


class StorageEventSource(Protocol):
    async def receive(
        self, *, maximum: int = 10, wait_seconds: int = 20
    ) -> list[dict[str, Any]]: ...

    async def acknowledge(self, receipt_handle: str) -> None: ...


class SQSEventSource:
    """SQS-compatible source; works with AWS SQS and compatible queue endpoints."""

    def __init__(self, client: BaseClient, queue_url: str) -> None:
        self.client = client
        self.queue_url = queue_url

    async def receive(self, *, maximum: int = 10, wait_seconds: int = 20) -> list[dict[str, Any]]:
        response = await asyncio.to_thread(
            self.client.receive_message,
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=min(max(maximum, 1), 10),
            WaitTimeSeconds=min(max(wait_seconds, 0), 20),
        )
        result = []
        for message in response.get("Messages", []):
            body = json.loads(message["Body"])
            if "Message" in body:
                body = json.loads(body["Message"])
            result.append(
                {
                    "receipt_handle": message["ReceiptHandle"],
                    "message_id": message.get("MessageId"),
                    "payload": body,
                }
            )
        return result

    async def acknowledge(self, receipt_handle: str) -> None:
        await asyncio.to_thread(
            self.client.delete_message,
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt_handle,
        )
