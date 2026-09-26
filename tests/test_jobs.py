import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from backend_foundation.core.config import Settings
from backend_foundation.core.modules import JobHandlerSpec, JobTypeSpec
from backend_foundation.modules.jobs.dispatcher import dispatch_once
from backend_foundation.modules.jobs.models import (
    Job,
    JobAttempt,
    JobBatch,
    JobStatus,
    JobTypeVersion,
    OutboxMessage,
    TenantJobQuota,
)
from backend_foundation.modules.jobs.service import (
    JobConflictError,
    JobQuotaExceededError,
    JobService,
    PermanentJobError,
    RetryableJobError,
)
from backend_foundation.modules.jobs.worker import (
    _update_batch,
    build_job_activity,
    sync_job_types,
)


class FakeSession:
    def __init__(self, existing: Job | None = None) -> None:
        self.existing = existing
        self.added: list[object] = []
        self.deleted: list[object] = []

    async def scalar(self, _query: object) -> Job | None:
        return self.existing

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if isinstance(value, Job) and value.id is None:
                value.id = "job-id"

    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def delete(self, value: object) -> None:
        self.deleted.append(value)


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_rejects_payload_conflict() -> None:
    service = JobService()
    session = FakeSession()
    created = await service.enqueue(  # type: ignore[arg-type]
        session, job_type="example", payload={"value": 1}, idempotency_key="key"
    )
    assert created.id == "job-id"
    assert len([item for item in session.added if isinstance(item, OutboxMessage)]) == 1

    existing = FakeSession(created)
    assert (
        await service.enqueue(  # type: ignore[arg-type]
            existing, job_type="example", payload={"value": 1}, idempotency_key="key"
        )
        is created
    )
    with pytest.raises(JobConflictError, match="different payload"):
        await service.enqueue(  # type: ignore[arg-type]
            existing, job_type="example", payload={"value": 2}, idempotency_key="key"
        )


@pytest.mark.asyncio
async def test_retry_and_cancel_state_transitions() -> None:
    service = JobService()
    session = FakeSession()
    job = Job(
        id="job-id",
        job_type="example",
        payload={},
        payload_hash="hash",
        idempotency_scope="global",
        idempotency_key="key",
        status=JobStatus.FAILED,
        attempt=1,
        available_at=datetime.now(UTC),
    )
    await service.retry(session, job)  # type: ignore[arg-type]
    assert (job.status, job.attempt, job.error) == (JobStatus.PENDING, 2, None)
    assert isinstance(session.added[-1], OutboxMessage)
    with pytest.raises(JobConflictError, match="Only failed"):
        await service.retry(session, job)  # type: ignore[arg-type]
    await service.cancel(job)
    assert job.status == JobStatus.CANCELLED
    job.status = JobStatus.RUNNING
    await service.cancel(job)
    assert job.status == JobStatus.CANCELLING
    job.status = JobStatus.SUCCEEDED
    assert await service.cancel(job) is job


@pytest.mark.asyncio
async def test_schedule_lifecycle() -> None:
    service = JobService()
    session = FakeSession()
    client = Mock()
    client.create_schedule = AsyncMock()
    handle = Mock()
    handle.update = AsyncMock()
    handle.pause = AsyncMock()
    handle.unpause = AsyncMock()
    handle.delete = AsyncMock()
    client.get_schedule_handle.return_value = handle

    record = await service.create_schedule(  # type: ignore[arg-type]
        session,
        client,
        name="daily",
        cron="0 0 * * *",
        job_type="example",
        payload={"value": 1},
        task_queue="queue",
        organization_id="org",
    )
    assert record.temporal_schedule_id.startswith("job-schedule/")
    client.create_schedule.assert_awaited_once()
    await service.update_schedule(record, client, task_queue="queue", cron="30 0 * * *")
    await service.pause_schedule(record, client, "pause")
    assert record.paused
    await service.resume_schedule(record, client, "resume")
    assert not record.paused
    await service.delete_schedule(session, record, client)  # type: ignore[arg-type]
    assert session.deleted == [record]
    assert handle.update.await_count == 1
    handle.pause.assert_awaited_once_with(note="pause")
    handle.unpause.assert_awaited_once_with(note="resume")
    handle.delete.assert_awaited_once()


def test_payload_hash_is_order_independent() -> None:
    from backend_foundation.modules.jobs.service import _payload_hash

    assert _payload_hash({"a": 1, "b": 2}) == _payload_hash({"b": 2, "a": 1})


class ActivitySession:
    def __init__(self, job: Job) -> None:
        self.job = job
        self.attempt: JobAttempt | None = None
        self.scalar_calls = 0

    async def scalar(self, _query: object) -> object | None:
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return self.job
        return self.attempt

    def add(self, value: object) -> None:
        if isinstance(value, JobAttempt):
            self.attempt = value

    async def get(self, _model: object, _identity: str) -> Job:
        return self.job

    async def commit(self) -> None:
        pass


class ActivityFactory:
    def __init__(self, session: ActivitySession) -> None:
        self.session = session

    @asynccontextmanager
    async def __call__(self):
        yield self.session


@pytest.mark.asyncio
async def test_job_activity_records_success_and_failure() -> None:
    job = Job(
        id="job-id",
        job_type="example",
        payload={"value": 1},
        payload_hash="hash",
        idempotency_scope="global",
        idempotency_key="key",
        status=JobStatus.DISPATCHED,
        attempt=1,
        available_at=datetime.now(UTC),
    )

    async def succeed(job_id: str, payload: dict[str, object]) -> dict[str, object]:
        assert job_id == "job-id" and payload == {"value": 1}
        return {"ok": True}

    session = ActivitySession(job)
    execute = build_job_activity(  # type: ignore[arg-type]
        ActivityFactory(session), (JobHandlerSpec("example", succeed),)
    )
    assert await execute({"job_id": "job-id", "attempt": 1}) == {"ok": True}
    assert job.status == JobStatus.SUCCEEDED
    assert session.attempt is not None and session.attempt.finished_at is not None

    async def fail(_job_id: str, _payload: dict[str, object]) -> None:
        raise RuntimeError("handler failed")

    failed = Job(
        id="failed-id",
        job_type="example",
        payload={},
        payload_hash="hash",
        idempotency_scope="global",
        idempotency_key="failed",
        status=JobStatus.DISPATCHED,
        attempt=1,
        available_at=datetime.now(UTC),
    )
    failed_session = ActivitySession(failed)
    execute_failed = build_job_activity(  # type: ignore[arg-type]
        ActivityFactory(failed_session), (JobHandlerSpec("example", fail),)
    )
    with pytest.raises(RuntimeError, match="handler failed"):
        await execute_failed({"job_id": "failed-id", "attempt": 1})
    assert failed.status == JobStatus.FAILED
    assert failed_session.attempt is not None
    assert failed_session.attempt.error == "handler failed"


@pytest.mark.asyncio
async def test_job_activity_cancellation_and_missing_handler() -> None:
    job = Job(
        id="cancel-id",
        job_type="cancel",
        payload={},
        payload_hash="hash",
        idempotency_scope="global",
        idempotency_key="cancel",
        status=JobStatus.DISPATCHED,
        attempt=1,
        available_at=datetime.now(UTC),
    )

    async def cancel(_job_id: str, _payload: dict[str, object]) -> None:
        raise asyncio.CancelledError

    session = ActivitySession(job)
    execute = build_job_activity(  # type: ignore[arg-type]
        ActivityFactory(session), (JobHandlerSpec("cancel", cancel),)
    )
    with pytest.raises(asyncio.CancelledError):
        await execute({"job_id": "cancel-id", "attempt": 1})
    assert job.status == JobStatus.CANCELLED
    assert session.attempt is not None and session.attempt.error == "cancelled"

    missing_job = Job(
        id="missing-handler",
        job_type="unknown",
        payload={},
        payload_hash="hash",
        idempotency_scope="global",
        idempotency_key="missing",
        status=JobStatus.DISPATCHED,
        attempt=1,
        available_at=datetime.now(UTC),
    )
    with pytest.raises(RuntimeError, match="No handler"):
        await build_job_activity(ActivityFactory(ActivitySession(missing_job)), ())(  # type: ignore[arg-type]
            {"job_id": missing_job.id, "attempt": 1}
        )


@pytest.mark.asyncio
async def test_dispatcher_marks_outbox_and_job(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(
        id="dispatch-id",
        job_type="example",
        payload={},
        payload_hash="hash",
        idempotency_scope="global",
        idempotency_key="dispatch",
        status=JobStatus.PENDING,
        attempt=1,
        available_at=datetime.now(UTC),
    )
    message = OutboxMessage(
        id="message-id",
        aggregate_id=job.id,
        payload={"job_id": job.id, "attempt": 1},
        available_at=datetime.now(UTC),
    )

    class Result:
        def all(self) -> list[OutboxMessage]:
            return [message]

    class DispatchSession:
        async def scalars(self, _query: object) -> Result:
            return Result()

        async def get(self, _model: object, _identity: str) -> Job:
            return job

        async def commit(self) -> None:
            pass

    session = DispatchSession()

    class Factory:
        @asynccontextmanager
        async def __call__(self):
            yield session

    engine = Mock()
    engine.dispose = AsyncMock()
    monkeypatch.setattr(
        "backend_foundation.modules.jobs.dispatcher.create_async_engine", lambda *_a, **_k: engine
    )
    monkeypatch.setattr(
        "backend_foundation.modules.jobs.dispatcher.async_sessionmaker", lambda *_a, **_k: Factory()
    )
    client = Mock()
    client.start_workflow = AsyncMock()
    settings = Settings(
        _env_file=None, database_url="postgresql+asyncpg://unused", app_profile="workflow"
    )
    assert await dispatch_once(settings, client) == 1
    assert job.status == JobStatus.DISPATCHED
    assert message.dispatched_at is not None
    client.start_workflow.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_job_quota_batch_and_error_types() -> None:
    class QuotaSession:
        def __init__(self, values: list[int]) -> None:
            self.values = iter(values)

        async def get(self, _model: object, _identity: str) -> TenantJobQuota | None:
            return None

        async def scalar(self, _query: object) -> int:
            return next(self.values)

    service = JobService()
    await service._check_enqueue_quota(QuotaSession([0, 0]), "org")  # type: ignore[arg-type]
    with pytest.raises(JobQuotaExceededError) as queued:
        await service._check_enqueue_quota(QuotaSession([1000]), "org")  # type: ignore[arg-type]
    assert queued.value.retry_after_seconds == 60
    with pytest.raises(JobQuotaExceededError, match="submission"):
        await service._check_enqueue_quota(QuotaSession([0, 1000]), "org")  # type: ignore[arg-type]

    session = FakeSession()
    service.enqueue = AsyncMock(
        side_effect=[
            Job(id="one", job_type="a", payload={}, payload_hash="h", idempotency_key="1"),
            Job(id="two", job_type="b", payload={}, payload_hash="h", idempotency_key="2"),
        ]
    )
    batch, jobs = await service.create_batch(  # type: ignore[arg-type]
        session,
        organization_id="org",
        created_by="user",
        items=[{"job_type": "a"}, {"job_type": "b", "priority": "high"}],
    )
    assert isinstance(batch, JobBatch) and [item.id for item in jobs] == ["one", "two"]
    with pytest.raises(ValueError, match="Batch size"):
        await service.create_batch(  # type: ignore[arg-type]
            session, organization_id="org", created_by="user", items=[]
        )
    assert RetryableJobError("again", code="transient").code == "transient"
    assert PermanentJobError("stop", code="invalid").code == "invalid"


@pytest.mark.asyncio
async def test_sync_job_types_and_batch_aggregation() -> None:
    class SyncSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.scalar_values: list[object | None] = [None]
            self.batch = JobBatch(id="batch", organization_id="org", created_by="user", total=1)

        async def scalar(self, _query: object) -> object | None:
            return self.scalar_values.pop(0) if self.scalar_values else 1

        async def scalars(self, _query: object) -> object:
            return type("Values", (), {"all": lambda self: []})()

        async def get(self, model: object, _identity: str) -> object | None:
            return self.batch if model is JobBatch else None

        def add(self, value: object) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            pass

        async def commit(self) -> None:
            pass

    sync_session = SyncSession()
    factory = ActivityFactory(sync_session)  # type: ignore[arg-type]

    async def handler(_job_id: str, _payload: dict[str, object]) -> None:
        return None

    await sync_job_types(  # type: ignore[arg-type]
        factory,
        (
            JobTypeSpec(
                "versioned",
                handler,
                version="2.0.0",
                maximum_attempts=2,
                concurrency_limit=1,
            ),
        ),
    )
    registered = next(item for item in sync_session.added if isinstance(item, JobTypeVersion))
    assert registered.policy["maximum_attempts"] == 2

    current = Job(
        id="job",
        organization_id="org",
        job_type="example",
        payload={},
        payload_hash="hash",
        idempotency_key="key",
        batch_id="batch",
        status=JobStatus.SUCCEEDED,
    )
    sync_session.scalar_values = [1, 0, 0, 0]
    await _update_batch(sync_session, current)  # type: ignore[arg-type]
    assert sync_session.batch.status == "completed"
