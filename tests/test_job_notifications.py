import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from backend_foundation.core.config import Settings
from backend_foundation.modules.jobs.models import Job, JobEvent, JobWebhook, WebhookDelivery
from backend_foundation.modules.jobs.notifications import (
    create_webhook,
    decrypt_secret,
    dispatch_webhooks_once,
    emit_job_event,
    encrypt_secret,
    validate_webhook_url,
)
from backend_foundation.modules.jobs.settings import get_jobs_settings


def test_webhook_secret_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    key = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    monkeypatch.setenv("JOBS_WEBHOOK_ENCRYPTION_KEY", key)
    get_jobs_settings.cache_clear()
    encrypted = encrypt_secret("only-returned-once")
    assert "only-returned-once" not in encrypted
    assert decrypt_secret(encrypted) == "only-returned-once"
    get_jobs_settings.cache_clear()


@pytest.mark.asyncio
async def test_webhook_dispatch_success(monkeypatch: pytest.MonkeyPatch) -> None:
    key = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    monkeypatch.setenv("JOBS_WEBHOOK_ENCRYPTION_KEY", key)
    get_jobs_settings.cache_clear()
    hook = JobWebhook(
        id="hook",
        organization_id="org",
        url="https://example.com/hook",
        event_types=["job.succeeded"],
        encrypted_secret=encrypt_secret("secret"),
        enabled=True,
    )
    event = JobEvent(
        id="event",
        job_id="job",
        organization_id="org",
        event_type="job.succeeded",
        payload={"ok": True},
        created_at=datetime.now(UTC),
    )
    delivery = WebhookDelivery(
        id="delivery",
        webhook_id=hook.id,
        event_id=event.id,
        status="pending",
        attempt=0,
        next_attempt_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )

    class Values:
        def all(self):
            return [delivery]

    class Session:
        async def scalars(self, _query):
            return Values()

        async def get(self, model, _identity):
            return hook if model is JobWebhook else event

        async def commit(self):
            pass

    class Factory:
        @asynccontextmanager
        async def __call__(self):
            yield Session()

    class Response:
        status_code = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def aiter_bytes(self):
            yield b"ok"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def stream(self, *_args, **_kwargs):
            return Response()

    engine = Mock(dispose=AsyncMock())
    monkeypatch.setattr(
        "backend_foundation.modules.jobs.notifications.create_async_engine",
        lambda *_a, **_k: engine,
    )
    monkeypatch.setattr(
        "backend_foundation.modules.jobs.notifications.async_sessionmaker",
        lambda *_a, **_k: Factory(),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.jobs.notifications.httpx.AsyncClient",
        lambda **_k: Client(),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.jobs.notifications.validate_webhook_url", AsyncMock()
    )
    assert await dispatch_webhooks_once(Settings(_env_file=None)) == 1
    assert delivery.status == "delivered"
    engine.dispose.assert_awaited_once()
    get_jobs_settings.cache_clear()


@pytest.mark.asyncio
async def test_webhook_rejects_non_https_and_private_addresses() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        await validate_webhook_url("http://example.com/hook")
    with pytest.raises(ValueError, match="non-public"):
        await validate_webhook_url("https://127.0.0.1/hook")


@pytest.mark.asyncio
async def test_webhook_creation_and_event_delivery_records(monkeypatch: pytest.MonkeyPatch) -> None:
    key = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    monkeypatch.setenv("JOBS_WEBHOOK_ENCRYPTION_KEY", key)
    get_jobs_settings.cache_clear()
    monkeypatch.setattr(
        "backend_foundation.modules.jobs.notifications.validate_webhook_url", AsyncMock()
    )

    class Values:
        def all(self):
            return [hook]

    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()
    session.scalars = AsyncMock(return_value=Values())
    hook, secret = await create_webhook(
        session,
        organization_id="org",
        url="https://example.com/hook",
        event_types=["job.succeeded", "job.succeeded"],
    )
    hook.id = "hook"
    assert decrypt_secret(hook.encrypted_secret) == secret
    assert hook.event_types == ["job.succeeded"]

    current = Job(
        id="job",
        organization_id="org",
        job_type="example",
        payload={},
        payload_hash="hash",
        idempotency_scope="org",
        idempotency_key="key",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    event = await emit_job_event(session, current, "job.succeeded", {"ok": True})
    assert isinstance(event, JobEvent)
    assert any(
        type(value).__name__ == "WebhookDelivery" for value in session.add.call_args_list[-1].args
    )
    get_jobs_settings.cache_clear()
