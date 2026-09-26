from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

import boto3  # type: ignore[import-untyped]
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend_foundation.modules.jobs.models import Job, JobAttempt, JobStatus
from backend_foundation.modules.jobs.service import JobService
from backend_foundation.modules.rbac.models import OrganizationMembership
from backend_foundation.modules.storage.models import StorageEventReceipt
from backend_foundation.modules.users.models import User

from .support import PYTHON, ROOT, IntegrationProject, free_port, wait_http
from .test_runtime_flows import register_verify_login, wait_for_job

pytestmark = pytest.mark.integration


async def _organization_id(factory: async_sessionmaker[Any], email: str) -> str:
    async with factory() as session:
        user = await session.scalar(select(User).where(User.normalized_email == email))
        assert user is not None
        membership = await session.scalar(
            select(OrganizationMembership).where(OrganizationMembership.user_id == user.id)
        )
        assert membership is not None
        return membership.organization_id


@pytest.mark.saas
async def test_real_minio_multipart_resume_refresh_tamper_and_concurrent_complete(
    full_runtime: IntegrationProject,
) -> None:
    project = full_runtime
    email = "multipart@example.com"
    token, _password = await register_verify_login(project, email)
    engine = create_async_engine(project.env["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    organization_id = await _organization_id(factory, email)
    api = f"http://127.0.0.1:{project.env['API_PORT']}"
    headers = {"Authorization": f"Bearer {token}"}
    part_size = 5 * 1024 * 1024
    payload = b"a" * part_size + b"resumed multipart tail"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            created = await client.post(
                f"{api}/organizations/{organization_id}/files/upload-sessions",
                headers=headers,
                json={
                    "filename": "large.txt",
                    "content_type": "text/plain",
                    "size": len(payload),
                },
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["mode"] == "multipart" and body["part_count"] == 2
            file_id = str(body["file_id"])
            revision_id = str(body["revision_id"])
            upload_id = str(body["upload_id"])
            stale_url = str(body["parts"][0]["upload_url"])

            await asyncio.sleep(2)
            try:
                expired = await client.put(stale_url, content=payload[:part_size])
                assert expired.status_code == 403
            except httpx.TransportError:
                # MinIO may close an expired streaming PUT before returning its XML error body.
                pass

            refreshed = await client.post(
                f"{api}/organizations/{organization_id}/files/{file_id}/uploads/"
                f"{upload_id}/part-urls",
                headers=headers,
                json={"part_numbers": [1]},
            )
            assert refreshed.status_code == 200, refreshed.text
            part_one = await client.put(
                refreshed.json()["parts"][0]["upload_url"], content=payload[:part_size]
            )
            assert part_one.status_code == 200, part_one.text
            etag_one = part_one.headers["etag"].strip('"')

        async with httpx.AsyncClient(timeout=30) as resumed_client:
            resumed = await resumed_client.get(
                f"{api}/organizations/{organization_id}/files/{file_id}/uploads/{upload_id}/parts",
                headers=headers,
            )
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["parts"] == [
                {"part_number": 1, "etag": etag_one, "size": part_size}
            ]
            refreshed = await resumed_client.post(
                f"{api}/organizations/{organization_id}/files/{file_id}/uploads/"
                f"{upload_id}/part-urls",
                headers=headers,
                json={"part_numbers": [2]},
            )
            part_two = await resumed_client.put(
                refreshed.json()["parts"][0]["upload_url"], content=payload[part_size:]
            )
            assert part_two.status_code == 200, part_two.text
            etag_two = part_two.headers["etag"].strip('"')
            complete_url = (
                f"{api}/organizations/{organization_id}/files/{file_id}/revisions/"
                f"{revision_id}/complete"
            )
            correct_parts = [
                {"part_number": 1, "etag": etag_one},
                {"part_number": 2, "etag": etag_two},
            ]
            tampered = await resumed_client.post(
                complete_url,
                headers=headers,
                json={"parts": [{"part_number": 1, "etag": "tampered"}, correct_parts[1]]},
            )
            assert tampered.status_code == 400
            assert "do not match" in tampered.text

            first, duplicate = await asyncio.gather(
                resumed_client.post(complete_url, headers=headers, json={"parts": correct_parts}),
                resumed_client.post(complete_url, headers=headers, json={"parts": correct_parts}),
            )
            assert first.status_code == 200, first.text
            assert duplicate.status_code == 200, duplicate.text

        async with factory() as session:
            processing = await session.scalar(
                select(Job).where(Job.idempotency_key == f"process:{revision_id}")
            )
            assert processing is not None
            processing_id = processing.id
        assert (await wait_for_job(factory, processing_id, JobStatus.SUCCEEDED)).result
    finally:
        await engine.dispose()


class _MinioEventHandler(BaseHTTPRequestHandler):
    events: queue.Queue[bytes]

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.events.put(self.rfile.read(length))
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format: str, *args: object) -> None:
        del args


def _collect_minio_records(events: queue.Queue[bytes], object_key: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 30
    records: list[dict[str, Any]] = []
    observed: list[tuple[str, str]] = []
    while time.monotonic() < deadline:
        try:
            payload = json.loads(events.get(timeout=1))
        except queue.Empty:
            continue
        for record in payload.get("Records", []):
            key = unquote_plus(str(record.get("s3", {}).get("object", {}).get("key", "")))
            observed.append((str(record.get("eventName")), key))
            if key == object_key:
                records.append(record)
        names = {str(record.get("eventName")) for record in records}
        if any("ObjectCreated" in name for name in names) and any(
            "ObjectRemoved" in name for name in names
        ):
            return records
    raise AssertionError(
        f"MinIO did not deliver create and remove events for {object_key}; observed={observed}"
    )


@pytest.mark.saas
async def test_real_minio_webhook_and_out_of_order_ingestion(
    full_runtime: IntegrationProject,
) -> None:
    project = full_runtime
    port = free_port()
    events: queue.Queue[bytes] = queue.Queue()
    handler = type("MinioEventHandler", (_MinioEventHandler,), {"events": events})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://host.docker.internal:{port}/events"
    configure = (
        'mc alias set local http://minio:9000 "$STORAGE_ACCESS_KEY" '
        '"$STORAGE_SECRET_KEY" && '
        f'mc admin config set local notify_webhook:integration endpoint="{endpoint}"'
    )
    try:
        project.compose(
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            "minio-init",
            "-c",
            configure,
        )
        project.compose("restart", "minio")
        wait_http(f"http://127.0.0.1:{project.env['STORAGE_HOST_PORT']}/minio/health/live")
        project.compose(
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            "minio-init",
            "-c",
            'attempt=0; until mc alias set local http://minio:9000 "$STORAGE_ACCESS_KEY" '
            '"$STORAGE_SECRET_KEY"; do attempt=$((attempt + 1)); '
            '[ "$attempt" -ge 30 ] && exit 1; sleep 1; done; '
            'mc event add "local/$STORAGE_BUCKET" '
            "arn:minio:sqs::integration:webhook --event put,delete",
        )

        client = boto3.client(
            "s3",
            endpoint_url=project.env["STORAGE_ENDPOINT_URL"],
            aws_access_key_id=project.env["STORAGE_ACCESS_KEY"],
            aws_secret_access_key=project.env["STORAGE_SECRET_KEY"],
            region_name="us-east-1",
        )
        object_key = f"events/{time.time_ns()}.txt"
        await asyncio.to_thread(
            client.put_object,
            Bucket=project.env["STORAGE_BUCKET"],
            Key=object_key,
            Body=b"event",
            ContentType="text/plain",
        )
        await asyncio.to_thread(
            client.delete_object, Bucket=project.env["STORAGE_BUCKET"], Key=object_key
        )
        records = await asyncio.to_thread(_collect_minio_records, events, object_key)
        records.sort(key=lambda item: "ObjectRemoved" not in str(item.get("eventName")))

        secret = project.env["STORAGE_EVENT_WEBHOOK_SECRET"]
        api = f"http://127.0.0.1:{project.env['API_PORT']}"
        async with httpx.AsyncClient(timeout=20) as http:
            for record in records[:2]:
                body = json.dumps({"Records": [record]}, separators=(",", ":")).encode()
                signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                response = await http.post(
                    f"{api}/storage/events/s3",
                    content=body,
                    headers={"X-Storage-Signature": signature, "Content-Type": "application/json"},
                )
                assert response.status_code == 202, response.text

        engine = create_async_engine(project.env["DATABASE_URL"])
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                receipts = list(
                    (
                        await session.scalars(
                            select(StorageEventReceipt)
                            .where(StorageEventReceipt.object_key == object_key)
                            .order_by(StorageEventReceipt.created_at)
                        )
                    ).all()
                )
            assert len(receipts) == 2
            assert "ObjectRemoved" in receipts[0].event_type
            assert "ObjectCreated" in receipts[1].event_type
            assert all(receipt.processed for receipt in receipts)
        finally:
            await engine.dispose()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.jobs
async def test_temporal_permanent_error_and_exponential_backoff(
    full_runtime: IntegrationProject, tmp_path: Path
) -> None:
    project = full_runtime
    worker = next(item for item in project.processes if item.name == "worker")
    worker.stop()
    project.processes.remove(worker)
    integration_worker = project.start(
        "phase4-integration-worker",
        [str(PYTHON), str(ROOT / "tests/integration/job_worker.py")],
    )
    engine = create_async_engine(project.env["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            permanent = await JobService().enqueue(
                session,
                job_type="integration.permanent",
                payload={},
                idempotency_key="perm",
                maximum_attempts=5,
            )
            permanent_id = permanent.id
            attempt_log = tmp_path / "exponential-attempts.log"
            exponential = await JobService().enqueue(
                session,
                job_type="integration.exponential",
                payload={"attempt_log": str(attempt_log), "succeed_on": 3},
                idempotency_key="retry",
                maximum_attempts=3,
                initial_backoff_seconds=1,
                maximum_backoff_seconds=5,
                backoff_coefficient=2,
            )
            exponential_id = exponential.id
            await session.commit()

        failed = await wait_for_job(factory, permanent_id, JobStatus.DEAD_LETTERED)
        assert failed.error_class == "permanent"
        assert failed.error_code == "integration_permanent"
        succeeded = await wait_for_job(factory, exponential_id, JobStatus.SUCCEEDED)
        assert succeeded.result == {"handler_attempts": 3}

        timestamps = [
            float(value) for value in attempt_log.read_text(encoding="utf-8").splitlines()
        ]
        assert len(timestamps) == 3
        intervals = [timestamps[1] - timestamps[0], timestamps[2] - timestamps[1]]
        assert intervals[0] >= 0.8
        assert intervals[1] >= 1.6
        assert intervals[1] > intervals[0] * 1.5

        async with factory() as session:
            permanent_attempts = list(
                (
                    await session.scalars(
                        select(JobAttempt).where(JobAttempt.job_id == permanent_id)
                    )
                ).all()
            )
            retry_attempts = list(
                (
                    await session.scalars(
                        select(JobAttempt)
                        .where(JobAttempt.job_id == exponential_id)
                        .order_by(JobAttempt.attempt)
                    )
                ).all()
            )
        assert len(permanent_attempts) == 1
        assert len(retry_attempts) == 3
        assert [item.error_class for item in retry_attempts] == [
            "retryable",
            "retryable",
            None,
        ]
    finally:
        integration_worker.stop()
        if integration_worker in project.processes:
            project.processes.remove(integration_worker)
        if not any(item.name == "worker" for item in project.processes):
            project.start_worker()
        await engine.dispose()
