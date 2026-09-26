"""Private versioned object storage."""

from collections.abc import Sequence

from alembic import op
from {{PACKAGE_NAME}}.modules.storage.models import (
    FileAsset,
    FileRevision,
    MultipartUpload,
    StorageEventReceipt,
    StoredFile,
)

revision: str = "{{REVISION}}"
down_revision: str | None = "{{DOWN_REVISION}}"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (StoredFile, FileAsset, FileRevision, MultipartUpload, StorageEventReceipt)


def upgrade() -> None:
    bind = op.get_bind()
    for model in TABLES:
        model.__table__.create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for model in reversed(TABLES):
        model.__table__.drop(bind)
