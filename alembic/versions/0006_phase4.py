"""Phase four environment, storage, and jobs platform schema."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_phase4"
down_revision: str | None = "0005_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for _name, column in (
        (
            "job_version",
            sa.Column("job_version", sa.String(32), nullable=False, server_default="1.0.0"),
        ),
        ("generation", sa.Column("generation", sa.Integer(), nullable=False, server_default="1")),
        (
            "maximum_attempts",
            sa.Column("maximum_attempts", sa.Integer(), nullable=False, server_default="5"),
        ),
        (
            "timeout_seconds",
            sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="300"),
        ),
        (
            "initial_backoff_seconds",
            sa.Column("initial_backoff_seconds", sa.Integer(), nullable=False, server_default="1"),
        ),
        (
            "maximum_backoff_seconds",
            sa.Column(
                "maximum_backoff_seconds", sa.Integer(), nullable=False, server_default="300"
            ),
        ),
        (
            "backoff_coefficient",
            sa.Column("backoff_coefficient", sa.Integer(), nullable=False, server_default="2"),
        ),
        ("concurrency_limit", sa.Column("concurrency_limit", sa.Integer(), nullable=True)),
        ("priority", sa.Column("priority", sa.String(16), nullable=False, server_default="normal")),
        ("batch_id", sa.Column("batch_id", sa.String(36), nullable=True)),
        ("error_code", sa.Column("error_code", sa.String(128), nullable=True)),
        ("error_class", sa.Column("error_class", sa.String(32), nullable=True)),
        (
            "dead_lettered_at",
            sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        ),
    ):
        op.add_column("jobs", column)
    op.create_index("ix_jobs_priority", "jobs", ["priority"])
    op.create_index("ix_jobs_batch_id", "jobs", ["batch_id"])
    op.add_column("job_attempts", sa.Column("error_code", sa.String(128), nullable=True))
    op.add_column("job_attempts", sa.Column("error_class", sa.String(32), nullable=True))
    op.add_column("job_attempts", sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True))
    for column in (
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("calendar", sa.JSON(), nullable=True),
        sa.Column("exclusions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("overlap_policy", sa.String(32), nullable=False, server_default="skip"),
        sa.Column("catchup_window_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("jitter_seconds", sa.Integer(), nullable=False, server_default="0"),
    ):
        op.add_column("job_schedules", column)

    op.create_table(
        "job_type_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "version"),
    )
    op.create_index("ix_job_type_versions_name", "job_type_versions", ["name"])
    op.create_table(
        "tenant_job_quotas",
        sa.Column("organization_id", sa.String(36), primary_key=True),
        sa.Column("max_queued", sa.Integer(), nullable=False),
        sa.Column("max_running", sa.Integer(), nullable=False),
        sa.Column("max_submissions_per_hour", sa.Integer(), nullable=False),
        sa.Column("max_schedules", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "job_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("succeeded", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("cancelled", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_job_batches_organization_id", "job_batches", ["organization_id"])
    op.create_table(
        "job_execution_leases",
        sa.Column(
            "job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("organization_id", sa.String(36), nullable=True),
        sa.Column("job_type", sa.String(128), nullable=False),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_job_execution_leases_organization_id", "job_execution_leases", ["organization_id"]
    )
    op.create_index("ix_job_execution_leases_job_type", "job_execution_leases", ["job_type"])
    op.create_index("ix_job_execution_leases_expires_at", "job_execution_leases", ["expires_at"])
    op.create_table(
        "job_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("organization_id", sa.String(36), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])
    op.create_index("ix_job_events_organization_id", "job_events", ["organization_id"])
    op.create_index("ix_job_events_event_type", "job_events", ["event_type"])
    op.create_table(
        "job_webhooks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("event_types", sa.JSON(), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_job_webhooks_organization_id", "job_webhooks", ["organization_id"])
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "webhook_id",
            sa.String(36),
            sa.ForeignKey("job_webhooks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("job_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("webhook_id", "event_id"),
    )
    op.create_index("ix_webhook_deliveries_webhook_id", "webhook_deliveries", ["webhook_id"])
    op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"])
    op.create_table(
        "platform_role_assignments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "role"),
    )
    op.create_index(
        "ix_platform_role_assignments_user_id", "platform_role_assignments", ["user_id"]
    )

    op.create_table(
        "file_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("current_revision_id", sa.String(36), nullable=True),
        sa.Column("pending_retention_days", sa.Integer(), nullable=False),
        sa.Column("deleted_retention_days", sa.Integer(), nullable=False),
        sa.Column("noncurrent_retention_days", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_file_assets_organization_id", "file_assets", ["organization_id"])
    op.create_index("ix_file_assets_created_by", "file_assets", ["created_by"])
    op.create_table(
        "file_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "file_id",
            sa.String(36),
            sa.ForeignKey("file_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False, unique=True),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("detected_content_type", sa.String(255), nullable=True),
        sa.Column("expected_size", sa.BigInteger(), nullable=False),
        sa.Column("expected_checksum", sa.String(128), nullable=True),
        sa.Column("actual_size", sa.BigInteger(), nullable=True),
        sa.Column("checksum", sa.String(128), nullable=True),
        sa.Column("media_metadata", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("file_id", "version"),
    )
    op.create_index("ix_file_revisions_file_id", "file_revisions", ["file_id"])
    op.create_index("ix_file_revisions_status", "file_revisions", ["status"])
    op.create_table(
        "multipart_uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "revision_id",
            sa.String(36),
            sa.ForeignKey("file_revisions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider_upload_id", sa.String(1024), nullable=False, unique=True),
        sa.Column("part_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_multipart_uploads_revision_id", "multipart_uploads", ["revision_id"])
    op.create_index("ix_multipart_uploads_expires_at", "multipart_uploads", ["expires_at"])
    op.create_table(
        "storage_event_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("deduplication_key", sa.String(255), nullable=False, unique=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_storage_event_receipts_object_key", "storage_event_receipts", ["object_key"]
    )

    op.execute("""
        INSERT INTO file_assets
            (id, organization_id, created_by, original_name, current_revision_id,
             pending_retention_days, deleted_retention_days, noncurrent_retention_days,
             deleted_at, created_at)
        SELECT id, organization_id, created_by, original_name,
               substring(id from 1 for 33) || 'r01', 1, 30, 90, deleted_at, created_at
        FROM stored_files
    """)
    op.execute("""
        INSERT INTO file_revisions
            (id, file_id, version, object_key, content_type, detected_content_type,
             expected_size, expected_checksum, actual_size, checksum, media_metadata,
             status, quarantine_reason, created_at, deleted_at)
        SELECT substring(id from 1 for 33) || 'r01', id, 1, object_key, content_type,
               content_type, expected_size, expected_checksum, actual_size, checksum,
               NULL, status, NULL, created_at, deleted_at
        FROM stored_files
    """)


def downgrade() -> None:
    op.drop_table("storage_event_receipts")
    op.drop_table("multipart_uploads")
    op.drop_table("file_revisions")
    op.drop_table("file_assets")
    op.drop_table("platform_role_assignments")
    op.drop_table("webhook_deliveries")
    op.drop_table("job_webhooks")
    op.drop_table("job_events")
    op.drop_table("job_execution_leases")
    op.drop_table("job_batches")
    op.drop_table("tenant_job_quotas")
    op.drop_table("job_type_versions")
    for name in (
        "jitter_seconds",
        "catchup_window_seconds",
        "overlap_policy",
        "exclusions",
        "calendar",
        "timezone",
    ):
        op.drop_column("job_schedules", name)
    for name in ("retry_at", "error_class", "error_code"):
        op.drop_column("job_attempts", name)
    op.drop_index("ix_jobs_batch_id", table_name="jobs")
    op.drop_index("ix_jobs_priority", table_name="jobs")
    for name in (
        "dead_lettered_at",
        "error_class",
        "error_code",
        "batch_id",
        "priority",
        "concurrency_limit",
        "backoff_coefficient",
        "maximum_backoff_seconds",
        "initial_backoff_seconds",
        "timeout_seconds",
        "maximum_attempts",
        "generation",
        "job_version",
    ):
        op.drop_column("jobs", name)
