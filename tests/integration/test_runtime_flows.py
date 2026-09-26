from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client

from backend_foundation.core.config import Settings
from backend_foundation.modules.jobs.dispatcher import dispatch_once
from backend_foundation.modules.jobs.models import (
    Job,
    JobAttempt,
    JobSchedule,
    JobStatus,
    OutboxMessage,
)
from backend_foundation.modules.jobs.service import JobService
from backend_foundation.modules.rbac.models import OrganizationMembership
from backend_foundation.modules.storage.models import StoredFile
from backend_foundation.modules.users.models import User

from .support import PYTHON, ROOT, IntegrationProject

pytestmark = pytest.mark.integration


async def wait_for_job(factory: async_sessionmaker[Any], job_id: str, status: str) -> Job:
    deadline = asyncio.get_running_loop().time() + 90
    while asyncio.get_running_loop().time() < deadline:
        async with factory() as session:
            job = await session.get(Job, job_id)
            if job is not None and job.status == status:
                return job
        await asyncio.sleep(0.25)
    raise AssertionError(f"Job {job_id} did not reach {status}")


def mailpit_message(address: str, mailpit_url: str) -> dict[str, Any] | None:
    response = httpx.get(f"{mailpit_url}/api/v1/messages", timeout=3)
    response.raise_for_status()
    for message in response.json().get("messages", []):
        recipients = str(message.get("To", ""))
        if address in recipients:
            message_id = message.get("ID") or message.get("Id") or message.get("id")
            detail = httpx.get(f"{mailpit_url}/api/v1/message/{message_id}", timeout=3)
            detail.raise_for_status()
            return dict(detail.json())
    return None


@pytest.mark.jobs
async def test_real_temporal_outbox_recovery_retry_and_schedule(
    full_runtime: IntegrationProject,
) -> None:
    project = full_runtime
    engine = create_async_engine(project.env["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    client = await Client.connect(
        project.env["TEMPORAL_HOST"], namespace=project.env["TEMPORAL_NAMESPACE"]
    )
    settings = Settings(_env_file=None, **project.env)
    dispatcher = next(item for item in project.processes if item.name == "dispatcher")
    worker = next(item for item in project.processes if item.name == "worker")
    worker.stop()
    project.processes.remove(worker)
    integration_worker = project.start(
        "integration-worker", [str(PYTHON), str(ROOT / "tests/integration/job_worker.py")]
    )
    try:
        async with factory() as session:
            job = await JobService().enqueue(
                session,
                job_type="email.send",
                payload={
                    "to": "jobs@example.test",
                    "template": "security_notice",
                    "context": {"message": "integration"},
                },
                idempotency_key="real-outbox",
            )
            job_id = job.id
            await session.commit()
        completed = await wait_for_job(factory, job_id, JobStatus.SUCCEEDED)
        assert completed.result == {"accepted": True}

        dispatcher.stop()
        project.processes.remove(dispatcher)

        async with factory() as session:
            outbox = await session.scalar(
                select(OutboxMessage).where(OutboxMessage.aggregate_id == job_id)
            )
            assert outbox is not None
            outbox.dispatched_at = None
            await session.commit()
        assert await dispatch_once(settings, client) == 1
        async with factory() as session:
            replayed = await session.get(Job, job_id)
            attempts = list(
                (await session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))).all()
            )
            assert replayed is not None and replayed.status == JobStatus.SUCCEEDED
            assert len(attempts) == 1

            replayed.status = JobStatus.FAILED
            await JobService().retry(session, replayed)
            await session.commit()
        # A still-draining dispatcher may have claimed this outbox row after its
        # shutdown signal; the state assertion below verifies the retry itself.
        assert await dispatch_once(settings, client) in (0, 1)
        retried = await wait_for_job(factory, job_id, JobStatus.SUCCEEDED)
        assert retried.attempt == 2

        async with factory() as session:
            flaky = await JobService().enqueue(
                session,
                job_type="integration.flaky",
                payload={"succeed_on": 3},
                idempotency_key="temporal-retry",
            )
            flaky_id = flaky.id
            await session.commit()
        assert await dispatch_once(settings, client) == 1
        retried_by_temporal = await wait_for_job(factory, flaky_id, JobStatus.SUCCEEDED)
        assert retried_by_temporal.result == {"handler_attempts": 3}
        async with factory() as session:
            attempts = list(
                (
                    await session.scalars(select(JobAttempt).where(JobAttempt.job_id == flaky_id))
                ).all()
            )
            assert [attempt.attempt for attempt in attempts] == [1, 2, 3]

        async with factory() as session:
            running = await JobService().enqueue(
                session,
                job_type="integration.slow",
                payload={},
                idempotency_key="temporal-cancel-running",
            )
            running_id = running.id
            await session.commit()
        assert await dispatch_once(settings, client) == 1
        await wait_for_job(factory, running_id, JobStatus.RUNNING)
        await client.get_workflow_handle(f"job/{running_id}/1").cancel()
        await wait_for_job(factory, running_id, JobStatus.CANCELLED)

        async with factory() as session:
            pending = await JobService().enqueue(
                session,
                job_type="email.send",
                payload={
                    "to": "cancel@example.test",
                    "template": "security_notice",
                    "context": {},
                },
                idempotency_key="cancel-pending",
                available_at=datetime(2099, 1, 1, tzinfo=UTC),
            )
            await JobService().cancel(pending)
            await session.commit()
            assert pending.status == JobStatus.CANCELLED

        async with factory() as session:
            schedule = await JobService().create_schedule(
                session,
                client,
                name="integration schedule",
                cron="0 0 1 1 *",
                job_type="email.send",
                payload={
                    "to": "schedule@example.test",
                    "template": "security_notice",
                    "context": {"message": "scheduled"},
                },
                task_queue=project.env["TEMPORAL_TASK_QUEUE"],
            )
            schedule_id = schedule.id
            temporal_id = schedule.temporal_schedule_id
            await session.commit()
        handle = client.get_schedule_handle(temporal_id)
        await handle.pause(note="integration")
        await handle.unpause(note="integration")
        await handle.trigger()

        deadline = asyncio.get_running_loop().time() + 90
        scheduled_job: Job | None = None
        while asyncio.get_running_loop().time() < deadline:
            await dispatch_once(settings, client)
            async with factory() as session:
                scheduled_job = await session.scalar(
                    select(Job).where(Job.idempotency_key.like(f"{schedule_id}:%"))
                )
                if scheduled_job is not None and scheduled_job.status == JobStatus.SUCCEEDED:
                    break
            await asyncio.sleep(0.25)
        assert scheduled_job is not None and scheduled_job.status == JobStatus.SUCCEEDED
        async with factory() as session:
            record = await session.get(JobSchedule, schedule_id)
            assert record is not None
            await JobService().delete_schedule(session, record, client)
            await session.commit()
    finally:
        integration_worker.stop()
        if integration_worker in project.processes:
            project.processes.remove(integration_worker)
        if not any(item.name == "worker" for item in project.processes):
            project.start_worker()
        if not any(item.name == "dispatcher" for item in project.processes):
            project.start_dispatcher()
        await engine.dispose()


async def register_verify_login(project: IntegrationProject, email: str) -> tuple[str, str]:
    api = f"http://127.0.0.1:{project.env['API_PORT']}"
    mailpit = f"http://127.0.0.1:{project.env['EMAIL_UI_PORT']}"
    password = "Integration-password-123"
    async with httpx.AsyncClient(base_url=api, timeout=10) as client:
        response = await client.post(
            "/auth/register",
            json={"email": email, "password": password, "display_name": "Integration"},
        )
        assert response.status_code == 202, response.text
        deadline = asyncio.get_running_loop().time() + 90
        message = None
        while asyncio.get_running_loop().time() < deadline:
            message = await asyncio.to_thread(mailpit_message, email, mailpit)
            if message:
                break
            await asyncio.sleep(0.25)
        assert message is not None
        content = str(message)
        token_match = re.search(r"token=([A-Za-z0-9._~-]+)", content)
        assert token_match, content
        response = await client.post("/auth/verify-email", json={"token": token_match.group(1)})
        assert response.status_code == 200, response.text
        response = await client.post("/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        return str(response.json()["access_token"]), password


@pytest.mark.identity
async def test_registration_temporal_smtp_mailpit(full_runtime: IntegrationProject) -> None:
    token, _password = await register_verify_login(full_runtime, "identity@example.com")
    response = httpx.get(
        f"http://127.0.0.1:{full_runtime.env['API_PORT']}/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["email_verified"] is True


@pytest.mark.saas
async def test_minio_presign_complete_download_and_async_delete(
    full_runtime: IntegrationProject,
) -> None:
    project = full_runtime
    email = "storage@example.com"
    token, _password = await register_verify_login(project, email)
    engine = create_async_engine(project.env["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user = await session.scalar(select(User).where(User.normalized_email == email))
            assert user is not None
            membership = await session.scalar(
                select(OrganizationMembership).where(OrganizationMembership.user_id == user.id)
            )
            assert membership is not None
            organization_id = membership.organization_id
        data = b"real minio integration payload"
        checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
        headers = {"Authorization": f"Bearer {token}"}
        api = f"http://127.0.0.1:{project.env['API_PORT']}"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{api}/organizations/{organization_id}/files/uploads",
                headers=headers,
                json={
                    "filename": "payload.txt",
                    "content_type": "text/plain",
                    "size": len(data),
                    "checksum_sha256": checksum,
                },
            )
            assert response.status_code == 201, response.text
            file_id = str(response.json()["id"])
            upload_url = str(response.json()["upload_url"])
            upload = await client.put(
                upload_url,
                content=data,
                headers={"content-type": "text/plain", "x-amz-checksum-sha256": checksum},
            )
            assert upload.status_code in {200, 204}, upload.text
            response = await client.post(
                f"{api}/organizations/{organization_id}/files/{file_id}/complete",
                headers=headers,
            )
            assert response.status_code == 200, response.text
            response = await client.post(
                f"{api}/organizations/{organization_id}/files/{file_id}/download",
                headers=headers,
            )
            assert response.status_code == 200, response.text
            download_url = str(response.json()["download_url"])
            downloaded = await client.get(download_url)
            assert downloaded.content == data
            response = await client.delete(
                f"{api}/organizations/{organization_id}/files/{file_id}", headers=headers
            )
            assert response.status_code == 204, response.text

        async with factory() as session:
            stored = await session.get(StoredFile, file_id)
            assert stored is not None
            delete_job = await session.scalar(
                select(Job).where(Job.idempotency_key == f"delete:{file_id}")
            )
            assert delete_job is not None
            delete_job_id = delete_job.id
        await wait_for_job(factory, delete_job_id, JobStatus.SUCCEEDED)
        async with httpx.AsyncClient(timeout=10) as client:
            assert (await client.get(download_url)).status_code >= 400
    finally:
        await engine.dispose()
