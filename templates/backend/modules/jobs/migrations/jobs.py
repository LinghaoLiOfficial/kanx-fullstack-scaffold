"""Durable job platform."""

from collections.abc import Sequence

from alembic import op
from {{PACKAGE_NAME}}.modules.jobs.models import (
    Job,
    JobAttempt,
    JobBatch,
    JobEvent,
    JobExecutionLease,
    JobSchedule,
    JobTypeVersion,
    JobWebhook,
    OutboxMessage,
    PlatformRoleAssignment,
    TenantJobQuota,
    WebhookDelivery,
)

revision: str = "{{REVISION}}"
down_revision: str | None = "{{DOWN_REVISION}}"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    Job,
    JobAttempt,
    OutboxMessage,
    JobSchedule,
    JobTypeVersion,
    TenantJobQuota,
    JobExecutionLease,
    JobBatch,
    JobEvent,
    JobWebhook,
    WebhookDelivery,
    PlatformRoleAssignment,
)


def upgrade() -> None:
    bind = op.get_bind()
    for model in TABLES:
        model.__table__.create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for model in reversed(TABLES):
        model.__table__.drop(bind)
