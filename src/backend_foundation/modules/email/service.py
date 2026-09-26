from __future__ import annotations

from email.message import EmailMessage
from typing import Any
from uuid import uuid4

import aiosmtplib
from sqlalchemy.ext.asyncio import AsyncSession

from ..jobs.models import Job
from ..jobs.service import JobService
from .settings import get_email_settings

SUBJECTS = {
    "verify_email": "Verify your email",
    "reset_password": "Reset your password",
    "security_notice": "Security notice",
}


def render_template(name: str, context: dict[str, Any]) -> tuple[str, str]:
    if name not in SUBJECTS:
        raise ValueError(f"Unknown email template: {name}")
    lines = [SUBJECTS[name], ""]
    if name == "verify_email":
        lines.append(f"Verify your email: {context['url']}")
    elif name == "reset_password":
        lines.append(f"Reset your password: {context['url']}")
    else:
        lines.append(str(context.get("message", "Your account security settings changed.")))
    return SUBJECTS[name], "\n".join(lines)


async def send_email_handler(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_email_settings()
    subject, body = render_template(str(payload["template"]), dict(payload["context"]))
    message = EmailMessage()
    message["From"] = settings.from_address
    message["To"] = str(payload["to"])
    message["Subject"] = subject
    message["Message-ID"] = f"<{job_id}@backend-foundation>"
    message.set_content(body)
    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username or None,
        password=settings.smtp_password.get_secret_value() or None,
        use_tls=settings.use_tls,
    )
    return {"accepted": True}


class EmailService:
    def __init__(self, jobs: JobService | None = None) -> None:
        self.jobs = jobs or JobService()

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        to: str,
        template: str,
        context: dict[str, Any],
        idempotency_key: str | None = None,
        organization_id: str | None = None,
    ) -> Job:
        render_template(template, context)
        return await self.jobs.enqueue(
            session,
            job_type="email.send",
            payload={"to": to, "template": template, "context": context},
            idempotency_key=idempotency_key or str(uuid4()),
            organization_id=organization_id,
        )
