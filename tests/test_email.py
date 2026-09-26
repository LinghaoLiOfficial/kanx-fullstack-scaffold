from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from backend_foundation.modules.email.service import (
    EmailService,
    render_template,
    send_email_handler,
)
from backend_foundation.modules.email.settings import EmailSettings


def test_email_templates_are_pre_registered() -> None:
    subject, body = render_template("verify_email", {"url": "https://example.test/verify"})
    assert subject == "Verify your email"
    assert "https://example.test/verify" in body
    with pytest.raises(ValueError, match="Unknown email template"):
        render_template("arbitrary-html", {"html": "<script>"})


def test_smtp_password_is_secret() -> None:
    marker = "smtp" + "-redaction-marker"
    settings = EmailSettings(_env_file=None, smtp_password=marker)
    assert isinstance(settings.smtp_password, SecretStr)
    assert marker not in repr(settings)


@pytest.mark.asyncio
async def test_email_enqueue_uses_durable_job() -> None:
    jobs = Mock()
    expected = object()
    jobs.enqueue = AsyncMock(return_value=expected)
    service = EmailService(jobs)
    result = await service.enqueue(
        Mock(),
        to="person@example.test",
        template="security_notice",
        context={"message": "changed"},
        idempotency_key="stable",
        organization_id="org",
    )
    assert result is expected
    jobs.enqueue.assert_awaited_once()
    assert jobs.enqueue.await_args.kwargs["job_type"] == "email.send"


@pytest.mark.asyncio
async def test_email_handler_uses_stable_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    send = AsyncMock()
    monkeypatch.setattr("backend_foundation.modules.email.service.aiosmtplib.send", send)
    monkeypatch.setattr(
        "backend_foundation.modules.email.service.get_email_settings",
        lambda: EmailSettings(_env_file=None),
    )
    assert await send_email_handler(
        "job-id",
        {
            "to": "person@example.test",
            "template": "verify_email",
            "context": {"url": "https://example.test/verify"},
        },
    ) == {"accepted": True}
    message = send.await_args.args[0]
    assert message["Message-ID"] == "<job-id@backend-foundation>"
