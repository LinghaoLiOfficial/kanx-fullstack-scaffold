"""Private object storage metadata."""

from collections.abc import Sequence

from alembic import op
from backend_foundation.modules.storage.models import StoredFile

revision: str = "0004_storage"
down_revision: str | None = "0003_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    StoredFile.__table__.create(op.get_bind())


def downgrade() -> None:
    StoredFile.__table__.drop(op.get_bind())
