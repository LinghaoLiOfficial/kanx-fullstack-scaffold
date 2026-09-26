from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from ..database.session import get_session
from ..jobs.models import (
    Job,
    JobAttempt,
    JobBatch,
    JobExecutionLease,
    JobPriority,
    JobSchedule,
    JobStatus,
    JobTypeVersion,
    JobWebhook,
    PlatformRoleAssignment,
    TenantJobQuota,
)
from ..jobs.notifications import create_webhook
from ..jobs.service import JobConflictError, JobQuotaExceededError, JobService
from ..rbac.service import AuthorizationError, RoleService
from .dependencies import AuthPrincipal, require_verified_user

router = APIRouter(prefix="/jobs", tags=["jobs"])
management_router = APIRouter(prefix="/organizations/{organization_id}/jobs", tags=["job-admin"])
admin_router = APIRouter(prefix="/admin/jobs", tags=["platform-job-admin"])


class BatchItem(BaseModel):
    job_type: str = Field(min_length=1, max_length=128)
    job_version: str = "1.0.0"
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    priority: JobPriority = JobPriority.NORMAL


class BatchRequest(BaseModel):
    items: list[BatchItem] = Field(min_length=1, max_length=1000)


class QuotaRequest(BaseModel):
    max_queued: int = Field(ge=1, le=1_000_000)
    max_running: int = Field(ge=1, le=10_000)
    max_submissions_per_hour: int = Field(ge=1, le=10_000_000)
    max_schedules: int = Field(ge=0, le=100_000)


class WebhookRequest(BaseModel):
    url: str = Field(max_length=2048)
    event_types: list[str] = Field(min_length=1, max_length=20)


class ScheduleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    job_type: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    cron: str = ""
    calendar: dict[str, Any] | None = None
    exclusions: list[dict[str, Any]] = Field(default_factory=list)
    timezone: str = "UTC"
    overlap_policy: str = "skip"
    catchup_window_seconds: int = Field(default=900, ge=1)
    jitter_seconds: int = Field(default=0, ge=0)


async def _require_org_permission(
    session: AsyncSession, user: AuthPrincipal, organization_id: str, permission: str
) -> None:
    try:
        await RoleService().require(session, user.id, organization_id, permission)
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail="Permission denied") from error


async def _platform_admin(
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthPrincipal:
    assignment = await session.scalar(
        select(PlatformRoleAssignment).where(
            PlatformRoleAssignment.user_id == user.id,
            PlatformRoleAssignment.role == "jobs_admin",
        )
    )
    if assignment is None:
        raise HTTPException(status_code=403, detail="Platform job administrator required")
    return user


def _job_body(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "organization_id": job.organization_id,
        "type": job.job_type,
        "version": job.job_version,
        "status": job.status,
        "priority": job.priority,
        "attempt": job.attempt,
        "generation": job.generation,
        "result": job.result,
        "error": job.error,
        "error_code": job.error_code,
        "error_class": job.error_class,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


async def _job_for_user(
    job_id: str, user: AuthPrincipal, session: AsyncSession, permission: str
) -> Job:
    job = await session.get(Job, job_id)
    if job is None or job.organization_id is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        await RoleService().require(session, user.id, job.organization_id, permission)
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return job


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    job = await _job_for_user(job_id, user, session, "jobs:read")
    return {
        "id": job.id,
        "type": job.job_type,
        "status": job.status,
        "attempt": job.attempt,
        "result": job.result,
        "error": job.error,
    }


@router.post("/{job_id}/cancel", status_code=202)
async def cancel_job(
    job_id: str,
    request: Request,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    job = await _job_for_user(job_id, user, session, "jobs:cancel")
    await JobService().cancel(job)
    settings = request.app.state.settings
    if job.status == "cancelling":
        client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
        await client.get_workflow_handle(f"job/{job.id}/{job.attempt}").cancel()
    await session.commit()
    return {"id": job.id, "status": job.status}


@management_router.get("")
async def list_organization_jobs(
    organization_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: str | None = None,
    job_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    await _require_org_permission(session, user, organization_id, "jobs:read")
    query = select(Job).where(Job.organization_id == organization_id)
    if status:
        query = query.where(Job.status == status)
    if job_type:
        query = query.where(Job.job_type == job_type)
    jobs = (
        await session.scalars(query.order_by(Job.created_at.desc()).limit(min(max(limit, 1), 500)))
    ).all()
    return [_job_body(job) for job in jobs]


@management_router.get("/{job_id}/attempts")
async def list_job_attempts(
    organization_id: str,
    job_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    await _require_org_permission(session, user, organization_id, "jobs:read")
    job = await session.get(Job, job_id)
    if job is None or job.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Job not found")
    values = (
        await session.scalars(
            select(JobAttempt).where(JobAttempt.job_id == job.id).order_by(JobAttempt.attempt)
        )
    ).all()
    return [
        {
            "attempt": item.attempt,
            "started_at": item.started_at,
            "finished_at": item.finished_at,
            "error": item.error,
            "error_code": item.error_code,
            "error_class": item.error_class,
        }
        for item in values
    ]


@management_router.post("/{job_id}/retry", status_code=202)
async def retry_job(
    organization_id: str,
    job_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _require_org_permission(session, user, organization_id, "jobs:retry")
    job = await session.get(Job, job_id)
    if job is None or job.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        await JobService().retry(session, job)
        await session.commit()
    except JobConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _job_body(job)


@management_router.post("/batches", status_code=202)
async def create_batch(
    organization_id: str,
    body: BatchRequest,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _require_org_permission(session, user, organization_id, "jobs:manage")
    try:
        batch, jobs = await JobService().create_batch(
            session,
            organization_id=organization_id,
            created_by=user.id,
            items=[item.model_dump(exclude_none=True, mode="json") for item in body.items],
        )
        await session.commit()
    except JobQuotaExceededError as error:
        raise HTTPException(
            status_code=429,
            detail={"code": error.code, "retry_after": error.retry_after_seconds},
        ) from error
    return {"id": batch.id, "status": batch.status, "jobs": [job.id for job in jobs]}


@management_router.get("/batches/{batch_id}")
async def get_batch(
    organization_id: str,
    batch_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _require_org_permission(session, user, organization_id, "jobs:read")
    batch = await session.get(JobBatch, batch_id)
    if batch is None or batch.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Batch not found")
    jobs = (
        await session.scalars(select(Job).where(Job.batch_id == batch.id).order_by(Job.created_at))
    ).all()
    return {
        "id": batch.id,
        "status": batch.status,
        "total": batch.total,
        "counts": {
            "succeeded": sum(job.status == JobStatus.SUCCEEDED for job in jobs),
            "failed": sum(
                job.status in (JobStatus.FAILED, JobStatus.DEAD_LETTERED) for job in jobs
            ),
            "cancelled": sum(job.status == JobStatus.CANCELLED for job in jobs),
        },
        "jobs": [_job_body(job) for job in jobs],
    }


@management_router.post("/batches/{batch_id}/{action}", status_code=202)
async def control_batch(
    organization_id: str,
    batch_id: str,
    action: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _require_org_permission(session, user, organization_id, "jobs:manage")
    batch = await session.get(JobBatch, batch_id)
    if batch is None or batch.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Batch not found")
    jobs = (await session.scalars(select(Job).where(Job.batch_id == batch.id))).all()
    changed = 0
    for job in jobs:
        if action == "cancel" and job.status not in (
            JobStatus.SUCCEEDED,
            JobStatus.CANCELLED,
            JobStatus.ABANDONED,
        ):
            await JobService().cancel(job)
            changed += 1
        elif action == "retry_failed" and job.status in (
            JobStatus.FAILED,
            JobStatus.DEAD_LETTERED,
        ):
            await JobService().retry(session, job)
            changed += 1
    if action not in {"cancel", "retry_failed"}:
        raise HTTPException(status_code=400, detail="Unknown batch action")
    await session.commit()
    return {"id": batch.id, "action": action, "changed": changed}


@management_router.get("/quota/current")
async def get_quota(
    organization_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _require_org_permission(session, user, organization_id, "jobs:manage")
    quota = await session.get(TenantJobQuota, organization_id)
    return {
        "organization_id": organization_id,
        "max_queued": quota.max_queued if quota else 1000,
        "max_running": quota.max_running if quota else 10,
        "max_submissions_per_hour": quota.max_submissions_per_hour if quota else 1000,
        "max_schedules": quota.max_schedules if quota else 100,
    }


@management_router.post("/webhooks", status_code=201)
async def register_webhook(
    organization_id: str,
    body: WebhookRequest,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _require_org_permission(session, user, organization_id, "jobs:webhooks:manage")
    try:
        webhook, secret = await create_webhook(
            session,
            organization_id=organization_id,
            url=body.url,
            event_types=body.event_types,
        )
        await session.commit()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"id": webhook.id, "secret": secret, "secret_visible_once": True}


@management_router.get("/webhooks")
async def list_webhooks(
    organization_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    await _require_org_permission(session, user, organization_id, "jobs:webhooks:manage")
    values = (
        await session.scalars(
            select(JobWebhook).where(JobWebhook.organization_id == organization_id)
        )
    ).all()
    return [
        {
            "id": item.id,
            "url": item.url,
            "event_types": item.event_types,
            "enabled": item.enabled,
        }
        for item in values
    ]


@management_router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(
    organization_id: str,
    webhook_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    await _require_org_permission(session, user, organization_id, "jobs:webhooks:manage")
    webhook = await session.get(JobWebhook, webhook_id)
    if webhook is None or webhook.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Webhook not found")
    await session.delete(webhook)
    await session.commit()


@management_router.post("/schedules", status_code=201)
async def create_schedule(
    organization_id: str,
    body: ScheduleRequest,
    request: Request,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _require_org_permission(session, user, organization_id, "jobs:schedules:manage")
    quota = await session.get(TenantJobQuota, organization_id)
    count = len(
        (
            await session.scalars(
                select(JobSchedule).where(JobSchedule.organization_id == organization_id)
            )
        ).all()
    )
    if count >= (quota.max_schedules if quota else 100):
        raise HTTPException(status_code=429, detail="schedule_quota_exceeded")
    settings = request.app.state.settings
    try:
        record = await JobService().create_schedule(
            session,
            request.app.state.temporal,
            name=body.name,
            job_type=body.job_type,
            payload=body.payload,
            cron=body.cron,
            calendar=body.calendar,
            exclusions=body.exclusions,
            timezone=body.timezone,
            overlap_policy=body.overlap_policy,
            catchup_window_seconds=body.catchup_window_seconds,
            jitter_seconds=body.jitter_seconds,
            task_queue=settings.temporal_task_queue,
            organization_id=organization_id,
        )
        await session.commit()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"id": record.id, "paused": record.paused}


@management_router.get("/schedules")
async def list_schedules(
    organization_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    await _require_org_permission(session, user, organization_id, "jobs:read")
    values = (
        await session.scalars(
            select(JobSchedule).where(JobSchedule.organization_id == organization_id)
        )
    ).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "job_type": item.job_type,
            "cron": item.cron,
            "calendar": item.calendar,
            "timezone": item.timezone,
            "paused": item.paused,
        }
        for item in values
    ]


@management_router.put("/schedules/{schedule_id}")
async def update_schedule(
    organization_id: str,
    schedule_id: str,
    body: ScheduleRequest,
    request: Request,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _require_org_permission(session, user, organization_id, "jobs:schedules:manage")
    record = await session.get(JobSchedule, schedule_id)
    if record is None or record.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    try:
        await JobService().update_schedule(
            record,
            request.app.state.temporal,
            task_queue=request.app.state.settings.temporal_task_queue,
            cron=body.cron,
            payload=body.payload,
            timezone=body.timezone,
            calendar=body.calendar,
            exclusions=body.exclusions,
            overlap_policy=body.overlap_policy,
            catchup_window_seconds=body.catchup_window_seconds,
            jitter_seconds=body.jitter_seconds,
        )
        await session.commit()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"id": record.id, "paused": record.paused}


@management_router.post("/schedules/{schedule_id}/{action}")
async def control_schedule(
    organization_id: str,
    schedule_id: str,
    action: str,
    request: Request,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _require_org_permission(session, user, organization_id, "jobs:schedules:manage")
    record = await session.get(JobSchedule, schedule_id)
    if record is None or record.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    service = JobService()
    if action == "pause":
        await service.pause_schedule(record, request.app.state.temporal, "API request")
    elif action == "resume":
        await service.resume_schedule(record, request.app.state.temporal, "API request")
    elif action == "delete":
        await service.delete_schedule(session, record, request.app.state.temporal)
    else:
        raise HTTPException(status_code=400, detail="Unknown schedule action")
    await session.commit()
    return {"id": schedule_id, "action": action}


@admin_router.get("")
async def list_platform_jobs(
    _admin: Annotated[AuthPrincipal, Depends(_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: str | None = None,
    organization_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = select(Job)
    if status:
        query = query.where(Job.status == status)
    if organization_id:
        query = query.where(Job.organization_id == organization_id)
    jobs = (
        await session.scalars(query.order_by(Job.created_at.desc()).limit(min(max(limit, 1), 500)))
    ).all()
    return [_job_body(job) for job in jobs]


@admin_router.post("/{job_id}/redrive", status_code=202)
async def redrive_dead_letter(
    job_id: str,
    _admin: Annotated[AuthPrincipal, Depends(_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        await JobService().redrive(session, job)
        await session.commit()
    except JobConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _job_body(job)


@admin_router.post("/{job_id}/abandon")
async def abandon_dead_letter(
    job_id: str,
    _admin: Annotated[AuthPrincipal, Depends(_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    job = await session.get(Job, job_id)
    if job is None or job.status != JobStatus.DEAD_LETTERED:
        raise HTTPException(status_code=409, detail="Job is not dead-lettered")
    job.status = JobStatus.ABANDONED
    await session.commit()
    return _job_body(job)


@admin_router.get("/types/versions")
async def list_job_type_versions(
    _admin: Annotated[AuthPrincipal, Depends(_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    values = (await session.scalars(select(JobTypeVersion).order_by(JobTypeVersion.name))).all()
    return [
        {"name": item.name, "version": item.version, "enabled": item.enabled, "policy": item.policy}
        for item in values
    ]


@admin_router.get("/leases/active")
async def list_execution_leases(
    _admin: Annotated[AuthPrincipal, Depends(_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    values = (await session.scalars(select(JobExecutionLease))).all()
    return [
        {
            "job_id": item.job_id,
            "organization_id": item.organization_id,
            "job_type": item.job_type,
            "owner": item.owner,
            "expires_at": item.expires_at,
        }
        for item in values
    ]


@admin_router.put("/quotas/{organization_id}")
async def set_tenant_quota(
    organization_id: str,
    body: QuotaRequest,
    _admin: Annotated[AuthPrincipal, Depends(_platform_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    quota = await session.get(TenantJobQuota, organization_id)
    if quota is None:
        quota = TenantJobQuota(organization_id=organization_id)
        session.add(quota)
    for name, value in body.model_dump().items():
        setattr(quota, name, value)
    await session.commit()
    return {"organization_id": organization_id, **body.model_dump()}
