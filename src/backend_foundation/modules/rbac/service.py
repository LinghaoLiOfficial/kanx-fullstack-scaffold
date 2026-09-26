from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    MembershipRole,
    Organization,
    OrganizationMembership,
    Role,
    RolePermission,
)


class AuthorizationError(PermissionError):
    pass


class OrganizationService:
    async def create_personal(
        self, session: AsyncSession, *, user_id: str, display_name: str
    ) -> str:
        organization = Organization(
            name=f"{display_name}'s organization",
            slug=f"{slugify_organization(display_name)}-{user_id[:8]}",
        )
        session.add(organization)
        await session.flush()
        membership = OrganizationMembership(organization_id=organization.id, user_id=user_id)
        session.add(membership)
        roles: dict[str, Role] = {}
        for name in ("owner", "admin", "member"):
            role = Role(organization_id=organization.id, name=name, builtin=True)
            session.add(role)
            roles[name] = role
        await session.flush()
        session.add(RolePermission(role_id=roles["owner"].id, permission="*"))
        session.add(RolePermission(role_id=roles["admin"].id, permission="organization:manage"))
        session.add(RolePermission(role_id=roles["member"].id, permission="organization:read"))
        session.add(MembershipRole(membership_id=membership.id, role_id=roles["owner"].id))
        return organization.id


class MembershipService:
    async def remove(self, session: AsyncSession, membership_id: str) -> None:
        assignment = await session.scalar(
            select(MembershipRole)
            .join(Role, Role.id == MembershipRole.role_id)
            .where(MembershipRole.membership_id == membership_id, Role.name == "owner")
        )
        if assignment is not None:
            await RoleService().assert_not_last_owner(session, membership_id, assignment.role_id)
        membership = await session.get(OrganizationMembership, membership_id)
        if membership is not None:
            await session.delete(membership)


class RoleService:
    async def permissions(
        self, session: AsyncSession, user_id: str, organization_id: str
    ) -> set[str]:
        values = await session.scalars(
            select(RolePermission.permission)
            .join(Role, Role.id == RolePermission.role_id)
            .join(MembershipRole, MembershipRole.role_id == Role.id)
            .join(OrganizationMembership, OrganizationMembership.id == MembershipRole.membership_id)
            .where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id == organization_id,
            )
        )
        return set(values)

    async def require(
        self, session: AsyncSession, user_id: str, organization_id: str, permission: str
    ) -> None:
        permissions = await self.permissions(session, user_id, organization_id)
        namespace = permission.split(":", 1)[0] + ":*"
        if (
            "*" not in permissions
            and namespace not in permissions
            and permission not in permissions
        ):
            raise AuthorizationError(f"Missing permission: {permission}")

    async def assert_not_last_owner(
        self, session: AsyncSession, membership_id: str, role_id: str
    ) -> None:
        role = await session.get(Role, role_id)
        if role is None or role.name != "owner":
            return
        owners = await session.scalar(
            select(func.count())
            .select_from(MembershipRole)
            .join(Role, Role.id == MembershipRole.role_id)
            .where(Role.organization_id == role.organization_id, Role.name == "owner")
        )
        assignment = await session.scalar(
            select(MembershipRole).where(
                MembershipRole.membership_id == membership_id,
                MembershipRole.role_id == role_id,
            )
        )
        if assignment is not None and int(owners or 0) <= 1:
            raise AuthorizationError("An organization must retain at least one owner")

    async def remove_from_membership(
        self, session: AsyncSession, membership_id: str, role_id: str
    ) -> None:
        await self.assert_not_last_owner(session, membership_id, role_id)
        assignment = await session.scalar(
            select(MembershipRole).where(
                MembershipRole.membership_id == membership_id,
                MembershipRole.role_id == role_id,
            )
        )
        if assignment is not None:
            await session.delete(assignment)


def slugify_organization(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:80] or "organization"
