from ...core.modules import (
    ComposeCapability,
    JobTypeSpec,
    MigrationDescriptor,
    ModuleSpec,
)
from .models import (
    FileAsset,
    FileRevision,
    MultipartUpload,
    StorageEventReceipt,
    StoredFile,
)
from .router import events_router, router
from .service import (
    delete_file_handler,
    process_revision_handler,
    storage_health_checks,
    storage_lifecycle_handler,
    storage_lifespan,
)
from .settings import get_storage_settings

module = ModuleSpec(
    name="storage",
    requires=("auth", "rbac", "jobs"),
    routers=(router, events_router),
    lifespan=storage_lifespan,
    health_checks=storage_health_checks,
    models=(StoredFile, FileAsset, FileRevision, MultipartUpload, StorageEventReceipt),
    migrations=(MigrationDescriptor("storage"),),
    permissions=(
        "files:read",
        "files:write",
        "files:delete",
        "files:versions:*",
        "files:uploads:*",
        "files:quarantine:read",
    ),
    job_handlers=(
        JobTypeSpec("storage.delete", delete_file_handler, maximum_attempts=10),
        JobTypeSpec(
            "storage.process",
            process_revision_handler,
            timeout_seconds=1800,
            maximum_attempts=3,
            concurrency_limit=4,
        ),
        JobTypeSpec("storage.lifecycle", storage_lifecycle_handler, maximum_attempts=3),
    ),
    settings_factory=get_storage_settings,
    optional_env=(
        "STORAGE_ENDPOINT_URL",
        "STORAGE_ACCESS_KEY",
        "STORAGE_SECRET_KEY",
        "STORAGE_BUCKET",
        "STORAGE_MULTIPART_THRESHOLD",
        "STORAGE_MULTIPART_PART_SIZE",
        "STORAGE_CDN_BASE_URL",
        "STORAGE_CDN_SIGNING_SECRET",
        "STORAGE_EVENT_WEBHOOK_SECRET",
    ),
    optional_dependencies=("boto3>=1.40,<2",),
    compose_capabilities=(ComposeCapability("minio"),),
)
