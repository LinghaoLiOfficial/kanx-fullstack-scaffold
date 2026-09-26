from ...core.modules import ComposeCapability, JobHandlerSpec, ModuleSpec
from .service import send_email_handler
from .settings import get_email_settings

module = ModuleSpec(
    name="email",
    requires=("jobs",),
    job_handlers=(JobHandlerSpec("email.send", send_email_handler),),
    settings_factory=get_email_settings,
    optional_env=("EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT", "EMAIL_FROM_ADDRESS"),
    optional_dependencies=("aiosmtplib>=4,<6",),
    compose_capabilities=(ComposeCapability("mailpit"),),
)
