from ...core.modules import MigrationDescriptor, ModuleSpec
from .models import (
    MembershipRole,
    Organization,
    OrganizationMembership,
    Role,
    RolePermission,
)

module = ModuleSpec(
    name="rbac",
    requires=("users",),
    models=(Organization, OrganizationMembership, Role, RolePermission, MembershipRole),
    migrations=(MigrationDescriptor("rbac"),),
    permissions=("organization:*", "organization:read", "organization:manage"),
)
