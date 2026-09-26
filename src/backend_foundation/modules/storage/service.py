from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Any, Protocol
from uuid import uuid4

import boto3  # type: ignore[import-untyped]
from botocore.client import BaseClient  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ...core.config import Settings
from ...core.health import Check
from ..jobs.service import JobService
from .models import (
    FileAsset,
    FileRevision,
    FileStatus,
    MultipartUpload,
    StorageEventReceipt,
    StoredFile,
    UploadStatus,
)
from .settings import StorageSettings, get_storage_settings


def create_client(settings: StorageSettings) -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key.get_secret_value(),
        aws_secret_access_key=settings.secret_key.get_secret_value(),
        region_name=settings.region,
    )


class StorageProvider(Protocol):
    async def ensure_bucket(self, *, create: bool) -> None: ...

    async def presign_upload(
        self, object_key: str, content_type: str, checksum: str | None, expires: int
    ) -> str: ...

    async def head(self, object_key: str) -> dict[str, Any]: ...

    async def presign_download(self, object_key: str, expires: int) -> str: ...

    async def delete(self, object_key: str) -> None: ...

    async def create_multipart(self, object_key: str, content_type: str) -> str: ...

    async def presign_part(
        self, object_key: str, upload_id: str, part_number: int, expires: int
    ) -> str: ...

    async def list_parts(self, object_key: str, upload_id: str) -> list[dict[str, Any]]: ...

    async def complete_multipart(
        self, object_key: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> None: ...

    async def abort_multipart(self, object_key: str, upload_id: str) -> None: ...

    async def download_bytes(self, object_key: str, maximum: int) -> bytes: ...


class BotoStorageProvider:
    def __init__(self, settings: StorageSettings, client: BaseClient | None = None) -> None:
        self.settings = settings
        self.client = client or create_client(settings)

    async def ensure_bucket(self, *, create: bool) -> None:
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.settings.bucket)
        except ClientError as error:
            response = getattr(error, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            if not create or code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            await asyncio.to_thread(self.client.create_bucket, Bucket=self.settings.bucket)

    async def presign_upload(
        self, object_key: str, content_type: str, checksum: str | None, expires: int
    ) -> str:
        params = {
            "Bucket": self.settings.bucket,
            "Key": object_key,
            "ContentType": content_type,
            **({"ChecksumSHA256": checksum} if checksum else {}),
        }
        return str(
            await asyncio.to_thread(
                self.client.generate_presigned_url,
                "put_object",
                Params=params,
                ExpiresIn=expires,
            )
        )

    async def head(self, object_key: str) -> dict[str, Any]:
        return dict(
            await asyncio.to_thread(
                self.client.head_object,
                Bucket=self.settings.bucket,
                Key=object_key,
                ChecksumMode="ENABLED",
            )
        )

    async def presign_download(self, object_key: str, expires: int) -> str:
        return str(
            await asyncio.to_thread(
                self.client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self.settings.bucket, "Key": object_key},
                ExpiresIn=expires,
            )
        )

    async def delete(self, object_key: str) -> None:
        await asyncio.to_thread(
            self.client.delete_object, Bucket=self.settings.bucket, Key=object_key
        )

    async def create_multipart(self, object_key: str, content_type: str) -> str:
        response = await asyncio.to_thread(
            self.client.create_multipart_upload,
            Bucket=self.settings.bucket,
            Key=object_key,
            ContentType=content_type,
        )
        return str(response["UploadId"])

    async def presign_part(
        self, object_key: str, upload_id: str, part_number: int, expires: int
    ) -> str:
        return str(
            await asyncio.to_thread(
                self.client.generate_presigned_url,
                "upload_part",
                Params={
                    "Bucket": self.settings.bucket,
                    "Key": object_key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=expires,
            )
        )

    async def list_parts(self, object_key: str, upload_id: str) -> list[dict[str, Any]]:
        response = await asyncio.to_thread(
            self.client.list_parts,
            Bucket=self.settings.bucket,
            Key=object_key,
            UploadId=upload_id,
        )
        return [
            {
                "part_number": int(item["PartNumber"]),
                "etag": str(item["ETag"]).strip('"'),
                "size": int(item["Size"]),
            }
            for item in response.get("Parts", [])
        ]

    async def complete_multipart(
        self, object_key: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> None:
        await asyncio.to_thread(
            self.client.complete_multipart_upload,
            Bucket=self.settings.bucket,
            Key=object_key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": item["part_number"], "ETag": item["etag"]} for item in parts
                ]
            },
        )

    async def abort_multipart(self, object_key: str, upload_id: str) -> None:
        await asyncio.to_thread(
            self.client.abort_multipart_upload,
            Bucket=self.settings.bucket,
            Key=object_key,
            UploadId=upload_id,
        )

    async def download_bytes(self, object_key: str, maximum: int) -> bytes:
        response = await asyncio.to_thread(
            self.client.get_object,
            Bucket=self.settings.bucket,
            Key=object_key,
            Range=f"bytes=0-{maximum - 1}",
        )
        return bytes(await asyncio.to_thread(response["Body"].read, maximum))


async def delete_file_handler(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    del job_id
    settings = get_storage_settings()
    await BotoStorageProvider(settings).delete(str(payload["object_key"]))
    return {"deleted": True}


def _detected_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    try:
        data[:8192].decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain"


def _probe_media(data: bytes, content_type: str, settings: StorageSettings) -> dict[str, Any]:
    if content_type.startswith("image/"):
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = settings.max_image_pixels
        with tempfile.NamedTemporaryFile(suffix=".image") as stream:
            stream.write(data)
            stream.flush()
            with Image.open(stream.name) as image:
                image.verify()
                width, height = image.size
                if width * height > settings.max_image_pixels:
                    raise ValueError("Image exceeds pixel safety limit")
                return {"width": width, "height": height, "format": image.format}
    if content_type.startswith("video/"):
        with tempfile.NamedTemporaryFile(suffix=".video") as stream:
            stream.write(data)
            stream.flush()
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_name,width,height",
                    "-of",
                    "json",
                    stream.name,
                ],
                capture_output=True,
                text=True,
                timeout=settings.media_probe_timeout_seconds,
                check=True,
            )
        value: dict[str, Any] = json.loads(result.stdout)
        streams: list[dict[str, Any]] = value.get("streams", [])
        video: dict[str, Any] = next((item for item in streams if item.get("width")), {})
        return {
            "duration_seconds": float(value.get("format", {}).get("duration", 0)),
            "width": video.get("width"),
            "height": video.get("height"),
            "codec": video.get("codec_name"),
        }
    return {}


async def process_revision_handler(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    del job_id
    core = Settings()
    configured = get_storage_settings()
    engine = create_async_engine(core.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    provider = BotoStorageProvider(configured)
    try:
        async with factory() as session:
            revision = await session.get(FileRevision, str(payload["revision_id"]))
            if revision is None:
                raise ValueError("File revision not found")
            if revision.status == FileStatus.AVAILABLE:
                return {"status": revision.status}
            data = await provider.download_bytes(revision.object_key, revision.expected_size)
            detected = _detected_type(data)
            revision.detected_content_type = detected
            try:
                if detected != revision.content_type:
                    raise ValueError("Declared content type does not match file content")
                revision.media_metadata = await asyncio.to_thread(
                    _probe_media, data, detected, configured
                )
                revision.status = FileStatus.AVAILABLE
                revision.quarantine_reason = None
            except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                revision.status = FileStatus.QUARANTINED
                revision.quarantine_reason = str(error)[:2000]
            await session.commit()
            return {"status": revision.status, "content_type": detected}
    finally:
        await engine.dispose()


async def storage_lifecycle_handler(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    del job_id, payload
    core = Settings()
    configured = get_storage_settings()
    engine = create_async_engine(core.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    provider = BotoStorageProvider(configured)
    cleaned = 0
    try:
        async with factory() as session:
            now = datetime.now(UTC)
            uploads = list(
                (
                    await session.scalars(
                        select(MultipartUpload).where(
                            MultipartUpload.status == UploadStatus.ACTIVE,
                            MultipartUpload.expires_at <= now,
                        )
                    )
                ).all()
            )
            for upload in uploads:
                revision = await session.get(FileRevision, upload.revision_id)
                if revision is not None:
                    await provider.abort_multipart(revision.object_key, upload.provider_upload_id)
                upload.status = UploadStatus.EXPIRED
                cleaned += 1
            assets = list((await session.scalars(select(FileAsset))).all())
            for asset in assets:
                revisions = list(
                    (
                        await session.scalars(
                            select(FileRevision).where(FileRevision.file_id == asset.id)
                        )
                    ).all()
                )
                for revision in revisions:
                    deleted_due = (
                        asset.deleted_at is not None
                        and asset.deleted_at <= now - timedelta(days=asset.deleted_retention_days)
                    )
                    noncurrent_due = (
                        revision.id != asset.current_revision_id
                        and revision.created_at
                        <= now - timedelta(days=asset.noncurrent_retention_days)
                    )
                    if revision.status != FileStatus.DELETED and (deleted_due or noncurrent_due):
                        await provider.delete(revision.object_key)
                        revision.status = FileStatus.DELETED
                        revision.deleted_at = now
                        cleaned += 1
            await session.commit()
        return {"cleaned": cleaned}
    finally:
        await engine.dispose()


def storage_lifespan(app: FastAPI, _settings: Settings) -> AbstractAsyncContextManager[None]:
    @asynccontextmanager
    async def lifespan() -> AsyncIterator[None]:
        settings = get_storage_settings()
        provider = BotoStorageProvider(settings)
        await provider.ensure_bucket(
            create=settings.auto_create_bucket and not _settings.production
        )
        app.state.storage_provider = provider
        try:
            yield
        finally:
            app.state.storage_provider = None

    return lifespan()


def storage_health_checks(app: FastAPI) -> dict[str, Check]:
    async def check() -> None:
        await app.state.storage_provider.ensure_bucket(create=False)

    return {"storage": check}


class StorageService:
    def __init__(
        self,
        settings: StorageSettings | None = None,
        provider: StorageProvider | None = None,
        jobs: JobService | None = None,
    ) -> None:
        self.settings = settings or get_storage_settings()
        self.provider = provider or BotoStorageProvider(self.settings)
        self.jobs = jobs or JobService()

    async def create_version_upload(
        self,
        session: AsyncSession,
        *,
        organization_id: str,
        user_id: str,
        filename: str,
        content_type: str,
        size: int,
        checksum_sha256: str | None = None,
        file: FileAsset | None = None,
    ) -> tuple[FileAsset, FileRevision, MultipartUpload | None, dict[str, Any]]:
        self._validate_upload(content_type, size)
        safe_name = PurePath(filename).name[:200]
        if file is None:
            file = FileAsset(
                organization_id=organization_id,
                created_by=user_id,
                original_name=safe_name,
            )
            session.add(file)
            await session.flush()
            version = 1
        else:
            if file.organization_id != organization_id or file.deleted_at is not None:
                raise ValueError("File is not available")
            version = (
                int(
                    await session.scalar(
                        select(func.max(FileRevision.version)).where(
                            FileRevision.file_id == file.id
                        )
                    )
                    or 0
                )
                + 1
            )
        revision = FileRevision(
            file_id=file.id,
            version=version,
            object_key=f"{organization_id}/{file.id}/v{version}/{uuid4()}-{safe_name}",
            content_type=content_type,
            expected_size=size,
            expected_checksum=checksum_sha256,
        )
        session.add(revision)
        await session.flush()
        if size < self.settings.multipart_threshold:
            url = await self.provider.presign_upload(
                revision.object_key,
                content_type,
                checksum_sha256,
                self.settings.presign_seconds,
            )
            return file, revision, None, {"mode": "single", "upload_url": url}
        provider_id = await self.provider.create_multipart(revision.object_key, content_type)
        upload = MultipartUpload(
            revision_id=revision.id,
            provider_upload_id=provider_id,
            part_size=self.settings.multipart_part_size,
            expires_at=datetime.now(UTC) + timedelta(hours=self.settings.multipart_expiry_hours),
        )
        session.add(upload)
        await session.flush()
        count = math.ceil(size / upload.part_size)
        initial = list(range(1, min(count, 10) + 1))
        urls = await self.multipart_part_urls(revision, upload, initial)
        return (
            file,
            revision,
            upload,
            {
                "mode": "multipart",
                "upload_id": upload.id,
                "part_size": upload.part_size,
                "part_count": count,
                "parts": urls,
            },
        )

    def _validate_upload(self, content_type: str, size: int) -> None:
        allowed = {item.strip() for item in self.settings.allowed_content_types.split(",")}
        if content_type not in allowed:
            raise ValueError("Content type is not allowed")
        if size < 1 or size > self.settings.max_file_size:
            raise ValueError("File size is outside the allowed range")

    async def multipart_part_urls(
        self, revision: FileRevision, upload: MultipartUpload, part_numbers: list[int]
    ) -> list[dict[str, Any]]:
        if upload.status != UploadStatus.ACTIVE or upload.expires_at <= datetime.now(UTC):
            raise ValueError("Multipart upload is no longer active")
        total = math.ceil(revision.expected_size / upload.part_size)
        if (
            not part_numbers
            or len(part_numbers) > 100
            or any(number < 1 or number > total for number in part_numbers)
        ):
            raise ValueError("Invalid multipart part numbers")
        return [
            {
                "part_number": number,
                "upload_url": await self.provider.presign_part(
                    revision.object_key,
                    upload.provider_upload_id,
                    number,
                    self.settings.presign_seconds,
                ),
            }
            for number in sorted(set(part_numbers))
        ]

    async def multipart_parts(
        self, revision: FileRevision, upload: MultipartUpload
    ) -> list[dict[str, Any]]:
        return await self.provider.list_parts(revision.object_key, upload.provider_upload_id)

    async def complete_revision(
        self,
        session: AsyncSession,
        file: FileAsset,
        revision: FileRevision,
        upload: MultipartUpload | None,
        parts: list[dict[str, Any]] | None = None,
    ) -> FileRevision:
        if revision.status in (FileStatus.PROCESSING, FileStatus.AVAILABLE):
            return revision
        if upload is not None:
            provider_parts = await self.multipart_parts(revision, upload)
            submitted = sorted(parts or [], key=lambda item: int(item["part_number"]))
            normalized = [
                {
                    "part_number": int(item["part_number"]),
                    "etag": str(item["etag"]).strip('"'),
                }
                for item in submitted
            ]
            observed = [
                {"part_number": item["part_number"], "etag": item["etag"]}
                for item in provider_parts
            ]
            if normalized != observed:
                raise ValueError("Submitted multipart parts do not match object storage")
            upload.status = UploadStatus.COMPLETING
            await self.provider.complete_multipart(
                revision.object_key, upload.provider_upload_id, normalized
            )
            upload.status = UploadStatus.COMPLETED
        metadata = await self.provider.head(revision.object_key)
        actual_size = int(metadata["ContentLength"])
        if actual_size != revision.expected_size:
            raise ValueError("Uploaded object size does not match")
        actual_checksum = metadata.get("ChecksumSHA256")
        if revision.expected_checksum and actual_checksum != revision.expected_checksum:
            raise ValueError("Uploaded object checksum does not match")
        revision.actual_size = actual_size
        revision.checksum = str(actual_checksum or metadata.get("ETag", "")).strip('"')
        revision.status = FileStatus.PROCESSING
        file.current_revision_id = file.current_revision_id or revision.id
        await self.jobs.enqueue(
            session,
            job_type="storage.process",
            payload={"revision_id": revision.id},
            idempotency_key=f"process:{revision.id}",
            organization_id=file.organization_id,
            created_by=file.created_by,
        )
        return revision

    async def abort_multipart(self, revision: FileRevision, upload: MultipartUpload) -> None:
        if upload.status in (UploadStatus.ABORTED, UploadStatus.COMPLETED):
            return
        await self.provider.abort_multipart(revision.object_key, upload.provider_upload_id)
        upload.status = UploadStatus.ABORTED

    async def activate_revision(self, file: FileAsset, revision: FileRevision) -> None:
        if revision.file_id != file.id or revision.status != FileStatus.AVAILABLE:
            raise ValueError("Only an available revision can be activated")
        file.current_revision_id = revision.id

    async def revision_download_url(self, revision: FileRevision, filename: str) -> str:
        if revision.status != FileStatus.AVAILABLE:
            raise ValueError("File revision is not available")
        if not self.settings.cdn_base_url:
            return await self.provider.presign_download(
                revision.object_key, self.settings.presign_seconds
            )
        expires = int(datetime.now(UTC).timestamp()) + self.settings.presign_seconds
        path = "/" + revision.object_key.lstrip("/")
        message = f"{path}:{expires}".encode()
        signature = hmac.new(
            self.settings.cdn_signing_secret.get_secret_value().encode(),
            message,
            hashlib.sha256,
        ).hexdigest()
        safe_name = PurePath(filename).name
        return (
            f"{self.settings.cdn_base_url.rstrip('/')}{path}?expires={expires}"
            f"&signature={signature}&filename={safe_name}"
        )

    async def ingest_event(
        self, session: AsyncSession, *, source: str, event: dict[str, Any]
    ) -> StorageEventReceipt:
        key = str(event.get("id") or event.get("sequencer") or "")
        object_key = str(event["object_key"])
        event_type = str(event["event_type"])
        dedup = (
            key
            or hashlib.sha256(
                json.dumps([source, event_type, object_key, event], sort_keys=True).encode()
            ).hexdigest()
        )
        existing = await session.scalar(
            select(StorageEventReceipt).where(StorageEventReceipt.deduplication_key == dedup)
        )
        if existing is not None:
            return existing
        receipt = StorageEventReceipt(
            deduplication_key=dedup,
            source=source,
            event_type=event_type,
            object_key=object_key,
            payload=event,
            processed=True,
        )
        session.add(receipt)
        return receipt

    async def create_upload(
        self,
        session: AsyncSession,
        *,
        organization_id: str,
        user_id: str,
        filename: str,
        content_type: str,
        size: int,
        checksum_sha256: str | None = None,
    ) -> tuple[StoredFile, str]:
        allowed = {item.strip() for item in self.settings.allowed_content_types.split(",")}
        if content_type not in allowed:
            raise ValueError("Content type is not allowed")
        if size < 1 or size > self.settings.max_file_size:
            raise ValueError("File size is outside the allowed range")
        safe_name = PurePath(filename).name[:200]
        stored = StoredFile(
            organization_id=organization_id,
            created_by=user_id,
            object_key=f"{organization_id}/{uuid4()}/{safe_name}",
            original_name=safe_name,
            content_type=content_type,
            expected_size=size,
            expected_checksum=checksum_sha256,
        )
        session.add(stored)
        await session.flush()
        url = await self.provider.presign_upload(
            stored.object_key,
            content_type,
            checksum_sha256,
            self.settings.presign_seconds,
        )
        return stored, url

    async def complete_upload(self, stored: StoredFile) -> StoredFile:
        metadata = await self.provider.head(stored.object_key)
        actual_size = int(metadata["ContentLength"])
        if actual_size != stored.expected_size:
            raise ValueError("Uploaded object size does not match")
        actual_checksum = metadata.get("ChecksumSHA256")
        if stored.expected_checksum and actual_checksum != stored.expected_checksum:
            raise ValueError("Uploaded object checksum does not match")
        stored.actual_size = actual_size
        stored.checksum = str(actual_checksum or metadata.get("ETag", "")).strip('"')
        stored.status = FileStatus.AVAILABLE
        return stored

    async def download_url(self, stored: StoredFile) -> str:
        if stored.status != FileStatus.AVAILABLE:
            raise ValueError("File is not available")
        return await self.provider.presign_download(
            stored.object_key, self.settings.presign_seconds
        )

    async def delete(self, session: AsyncSession, stored: StoredFile) -> None:
        if stored.status == FileStatus.DELETED:
            return
        stored.status = FileStatus.DELETED
        stored.deleted_at = datetime.now(UTC)
        await self.jobs.enqueue(
            session,
            job_type="storage.delete",
            payload={"object_key": stored.object_key},
            idempotency_key=f"delete:{stored.id}",
            organization_id=stored.organization_id,
            created_by=stored.created_by,
        )
