from ...core.modules import MigrationDescriptor, ModuleSpec
from .models import User

module = ModuleSpec(
    name="users",
    requires=("database",),
    models=(User,),
    migrations=(MigrationDescriptor("users"),),
)
