from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend_foundation.modules.auth.dependencies import AuthPrincipal
from backend_foundation.modules.auth.jobs_router import (
    BatchItem,
    BatchRequest,
    QuotaRequest,
    ScheduleRequest,
    WebhookRequest,
    _job_for_user,
    _platform_admin,
    abandon_dead_letter,
    cancel_job,
    control_batch,
    control_schedule,
    create_batch,
    create_schedule,
    delete_webhook,
    get_batch,
    get_quota,
    list_execution_leases,
    list_job_attempts,
    list_job_type_versions,
    list_organization_jobs,
    list_platform_jobs,
    list_schedules,
    list_webhooks,
    redrive_dead_letter,
    register_webhook,
    retry_job,
    set_tenant_quota,
    update_schedule,
)
from backend_foundation.modules.jobs.models import (
    Job,
    JobAttempt,
    JobBatch,
    JobExecutionLease,
    JobPriority,
    JobSchedule,
    JobStatus,
    JobTypeVersion,
    JobWebhook,
    TenantJobQuota,
)


class Result:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def all(self) -> list[object]:
        return self.values


class Session:
    def __init__(self, values: list[object] | None = None) -> None:
        self.values = values or []
        self.objects: dict[tuple[object, str], object] = {}
        self.added: list[object] = []
        self.deleted: list[object] = []

    async def scalars(self, _query: object) -> Result:
        return Result(self.values)

    async def get(self, model: object, identity: str) -> object | None:
        return self.objects.get((model, identity))

    async def scalar(self, _query: object) -> object | None:
        return self.values[0] if self.values else None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def delete(self, value: object) -> None:
        self.deleted.append(value)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass


def principal() -> AuthPrincipal:
    return AuthPrincipal("user", "user@example.com", "User", datetime.now(UTC))


def job(status: JobStatus = JobStatus.FAILED) -> Job:
    return Job(
        id="job",
        organization_id="org",
        job_type="example",
        job_version="1.0.0",
        payload={},
        payload_hash="hash",
        idempotency_scope="org",
        idempotency_key="key",
        status=status,
        priority=JobPriority.NORMAL,
        attempt=1,
        generation=1,
        available_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_tenant_job_listing_attempt_retry_and_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend_foundation.modules.auth.jobs_router.RoleService.require", AsyncMock()
    )
    current = job()
    session = Session([current])
    body = await list_organization_jobs("org", principal(), session, limit=10)  # type: ignore[arg-type]
    assert body[0]["version"] == "1.0.0"

    attempt = JobAttempt(
        job_id=current.id,
        attempt=1,
        workflow_id="workflow",
        started_at=datetime.now(UTC),
    )
    session.values = [attempt]
    session.objects[(Job, current.id)] = current
    attempts = await list_job_attempts("org", current.id, principal(), session)  # type: ignore[arg-type]
    assert attempts[0]["attempt"] == 1

    result = await retry_job("org", current.id, principal(), session)  # type: ignore[arg-type]
    assert result["generation"] == 2

    batch = JobBatch(
        id="batch",
        organization_id="org",
        created_by="user",
        total=1,
        status="running",
    )
    current.batch_id = batch.id
    current.status = JobStatus.SUCCEEDED
    session.objects[(JobBatch, batch.id)] = batch
    session.values = [current]
    detail = await get_batch("org", batch.id, principal(), session)  # type: ignore[arg-type]
    assert detail["counts"]["succeeded"] == 1

    current.status = JobStatus.FAILED
    controlled = await control_batch("org", batch.id, "retry_failed", principal(), session)  # type: ignore[arg-type]
    assert controlled["changed"] == 1


@pytest.mark.asyncio
async def test_quota_webhook_and_schedule_views(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend_foundation.modules.auth.jobs_router.RoleService.require", AsyncMock()
    )
    session = Session()
    quota = await get_quota("org", principal(), session)  # type: ignore[arg-type]
    assert quota["max_running"] == 10

    webhook = JobWebhook(
        id="hook",
        organization_id="org",
        url="https://example.com/hook",
        event_types=["job.succeeded"],
        encrypted_secret="encrypted",
    )
    session.values = [webhook]
    assert (await list_webhooks("org", principal(), session))[0]["id"] == "hook"  # type: ignore[arg-type]
    session.objects[(JobWebhook, webhook.id)] = webhook
    await delete_webhook("org", webhook.id, principal(), session)  # type: ignore[arg-type]
    assert session.deleted == [webhook]

    create = AsyncMock(return_value=(webhook, "once"))
    monkeypatch.setattr("backend_foundation.modules.auth.jobs_router.create_webhook", create)
    registered = await register_webhook(
        "org",
        WebhookRequest(url=webhook.url, event_types=["job.succeeded"]),
        principal(),
        session,  # type: ignore[arg-type]
    )
    assert registered["secret_visible_once"] is True

    schedule = JobSchedule(
        id="schedule",
        temporal_schedule_id="temporal",
        organization_id="org",
        name="daily",
        job_type="example",
        payload={},
        cron="0 0 * * *",
        timezone="UTC",
    )
    session.values = [schedule]
    schedules = await list_schedules("org", principal(), session)  # type: ignore[arg-type]
    assert schedules[0]["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_platform_admin_views_and_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    current = job(JobStatus.DEAD_LETTERED)
    session = Session([current])
    session.objects[(Job, current.id)] = current
    assert (await list_platform_jobs(principal(), session))[0]["id"] == current.id  # type: ignore[arg-type]

    redriven = await redrive_dead_letter(current.id, principal(), session)  # type: ignore[arg-type]
    assert redriven["status"] == JobStatus.PENDING
    current.status = JobStatus.DEAD_LETTERED
    abandoned = await abandon_dead_letter(current.id, principal(), session)  # type: ignore[arg-type]
    assert abandoned["status"] == JobStatus.ABANDONED

    version = JobTypeVersion(name="example", version="1.0.0", enabled=True, policy={})
    session.values = [version]
    assert (await list_job_type_versions(principal(), session))[0]["name"] == "example"  # type: ignore[arg-type]

    lease = JobExecutionLease(
        job_id="job",
        job_type="example",
        owner="worker",
        expires_at=datetime.now(UTC),
    )
    session.values = [lease]
    assert (await list_execution_leases(principal(), session))[0]["owner"] == "worker"  # type: ignore[arg-type]

    session.values = []
    body = QuotaRequest(
        max_queued=20,
        max_running=3,
        max_submissions_per_hour=40,
        max_schedules=2,
    )
    saved = await set_tenant_quota("org", body, principal(), session)  # type: ignore[arg-type]
    assert saved["max_running"] == 3
    assert isinstance(session.added[-1], TenantJobQuota)


@pytest.mark.asyncio
async def test_job_access_batch_and_schedule_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    require = AsyncMock()
    monkeypatch.setattr("backend_foundation.modules.auth.jobs_router.RoleService.require", require)
    current = job(JobStatus.RUNNING)
    session = Session([current])
    session.objects[(Job, current.id)] = current
    assert await _job_for_user(current.id, principal(), session, "jobs:read") is current  # type: ignore[arg-type]

    cancel = AsyncMock(return_value=current)
    monkeypatch.setattr("backend_foundation.modules.auth.jobs_router.JobService.cancel", cancel)
    temporal_handle = Mock(cancel=AsyncMock())
    temporal = Mock(get_workflow_handle=Mock(return_value=temporal_handle))
    connect = AsyncMock(return_value=temporal)
    monkeypatch.setattr("backend_foundation.modules.auth.jobs_router.Client.connect", connect)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(temporal_host="host", temporal_namespace="namespace")
            )
        )
    )
    current.status = JobStatus.CANCELLING
    cancelled = await cancel_job(current.id, request, principal(), session)  # type: ignore[arg-type]
    assert cancelled["status"] == JobStatus.CANCELLING

    batch = JobBatch(id="batch", organization_id="org", created_by="user", total=1)
    batch_job = job(JobStatus.PENDING)
    create_batch_mock = AsyncMock(return_value=(batch, [batch_job]))
    monkeypatch.setattr(
        "backend_foundation.modules.auth.jobs_router.JobService.create_batch", create_batch_mock
    )
    batch_body = BatchRequest(items=[BatchItem(job_type="example", payload={})])
    created = await create_batch("org", batch_body, principal(), session)  # type: ignore[arg-type]
    assert created["id"] == "batch"

    schedule = JobSchedule(
        id="schedule",
        temporal_schedule_id="temporal",
        organization_id="org",
        name="daily",
        job_type="example",
        payload={},
        cron="0 0 * * *",
        timezone="UTC",
        paused=False,
    )
    session.values = []
    session.objects[(TenantJobQuota, "org")] = TenantJobQuota(
        organization_id="org", max_schedules=2
    )
    schedule_request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                temporal=Mock(), settings=SimpleNamespace(temporal_task_queue="queue")
            )
        )
    )
    create_schedule_mock = AsyncMock(return_value=schedule)
    update_schedule_mock = AsyncMock(return_value=schedule)
    pause = AsyncMock(return_value=schedule)
    monkeypatch.setattr(
        "backend_foundation.modules.auth.jobs_router.JobService.create_schedule",
        create_schedule_mock,
    )
    monkeypatch.setattr(
        "backend_foundation.modules.auth.jobs_router.JobService.update_schedule",
        update_schedule_mock,
    )
    monkeypatch.setattr(
        "backend_foundation.modules.auth.jobs_router.JobService.pause_schedule", pause
    )
    schedule_body = ScheduleRequest(
        name="daily", job_type="example", cron="0 0 * * *", timezone="UTC"
    )
    assert (
        await create_schedule(  # type: ignore[arg-type]
            "org", schedule_body, schedule_request, principal(), session
        )
    )["id"] == schedule.id
    session.objects[(JobSchedule, schedule.id)] = schedule
    assert (
        await update_schedule(  # type: ignore[arg-type]
            "org", schedule.id, schedule_body, schedule_request, principal(), session
        )
    )["id"] == schedule.id
    controlled = await control_schedule(  # type: ignore[arg-type]
        "org", schedule.id, "pause", schedule_request, principal(), session
    )
    assert controlled["action"] == "pause"


@pytest.mark.asyncio
async def test_platform_admin_dependency() -> None:
    assignment = SimpleNamespace(user_id="user", role="jobs_admin")
    session = Session([assignment])
    actor = principal()
    assert await _platform_admin(actor, session) == actor  # type: ignore[arg-type]
