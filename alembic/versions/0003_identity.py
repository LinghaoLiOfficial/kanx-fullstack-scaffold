"""Users, organizations, RBAC, and authentication."""

from collections.abc import Sequence

from alembic import op
from backend_foundation.modules.auth.models import AuthSession, AuthToken
from backend_foundation.modules.rbac.models import (
    MembershipRole,
    Organization,
    OrganizationMembership,
    Role,
    RolePermission,
)
from backend_foundation.modules.users.models import User

revision: str = "0003_identity"
down_revision: str | None = "0002_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    User.__table__,
    Organization.__table__,
    OrganizationMembership.__table__,
    Role.__table__,
    RolePermission.__table__,
    MembershipRole.__table__,
    AuthSession.__table__,
    AuthToken.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        table.create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(TABLES):
        table.drop(bind)
