from unittest.mock import AsyncMock, Mock

import pytest

from backend_foundation.modules.rbac.models import MembershipRole, OrganizationMembership, Role
from backend_foundation.modules.rbac.service import (
    AuthorizationError,
    MembershipService,
    OrganizationService,
    RoleService,
    slugify_organization,
)


class CreationSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.counter = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                self.counter += 1
                value.id = f"id-{self.counter}"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_personal_organization_has_builtin_roles() -> None:
    session = CreationSession()
    organization_id = await OrganizationService().create_personal(  # type: ignore[arg-type]
        session, user_id="user-12345678", display_name="A Person"
    )
    roles = [item for item in session.added if isinstance(item, Role)]
    assert organization_id.startswith("id-")
    assert {item.name for item in roles} == {"owner", "admin", "member"}
    assert slugify_organization(" Héllo, World! ") == "h-llo-world"


@pytest.mark.asyncio
async def test_permission_resolution_and_denial() -> None:
    session = Mock()
    session.scalars = AsyncMock(return_value=["organization:read", "files:*"])
    service = RoleService()
    assert await service.permissions(session, "user", "org") == {  # type: ignore[arg-type]
        "organization:read",
        "files:*",
    }
    await service.require(session, "user", "org", "files:delete")  # type: ignore[arg-type]
    with pytest.raises(AuthorizationError, match="Missing permission"):
        await service.require(session, "user", "org", "jobs:cancel")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_last_owner_protection_and_membership_removal() -> None:
    owner = Role(id="role", organization_id="org", name="owner", builtin=True)
    assignment = MembershipRole(id="assignment", membership_id="member", role_id="role")
    session = Mock()
    session.get = AsyncMock(side_effect=[owner, OrganizationMembership(id="member")])
    session.scalar = AsyncMock(side_effect=[assignment, 1, assignment])
    session.delete = AsyncMock()
    with pytest.raises(AuthorizationError, match="at least one owner"):
        await MembershipService().remove(session, "member")  # type: ignore[arg-type]

    non_owner = Role(id="member-role", organization_id="org", name="member", builtin=True)
    session.get = AsyncMock(return_value=non_owner)
    session.scalar = AsyncMock(return_value=assignment)
    await RoleService().remove_from_membership(  # type: ignore[arg-type]
        session, "member", "member-role"
    )
    session.delete.assert_awaited_with(assignment)
