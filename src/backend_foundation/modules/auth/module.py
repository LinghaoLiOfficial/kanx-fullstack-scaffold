from ...core.modules import MigrationDescriptor, ModuleSpec
from .jobs_router import admin_router, management_router
from .jobs_router import router as jobs_router
from .models import AuthSession, AuthToken
from .router import router
from .settings import get_auth_settings

module = ModuleSpec(
    name="auth",
    requires=("users", "rbac", "email"),
    routers=(router, jobs_router, management_router, admin_router),
    models=(AuthSession, AuthToken),
    migrations=(MigrationDescriptor("auth"),),
    settings_factory=get_auth_settings,
    required_env=("AUTH_JWT_SECRET",),
    optional_env=("AUTH_ALLOWED_ORIGINS", "AUTH_SECURE_COOKIES"),
    optional_dependencies=(
        "pwdlib[argon2]>=0.3,<1",
        "pyjwt>=2.10,<3",
        "email-validator>=2.2,<3",
    ),
)
