from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import ClientError
from PIL import Image
from pydantic import SecretStr

from backend_foundation.modules.storage.models import (
    FileAsset,
    FileRevision,
    FileStatus,
    MultipartUpload,
    StoredFile,
    UploadStatus,
)
from backend_foundation.modules.storage.service import (
    BotoStorageProvider,
    StorageService,
    _detected_type,
    _probe_media,
    process_revision_handler,
    storage_health_checks,
    storage_lifecycle_handler,
    storage_lifespan,
)
from backend_foundation.modules.storage.settings import StorageSettings


class FakeProvider:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {"ContentLength": 4, "ChecksumSHA256": "YQ=="}
        self.deleted: list[str] = []
        self.parts = [
            {"part_number": 1, "etag": "one", "size": 5},
            {"part_number": 2, "etag": "two", "size": 3},
        ]
        self.completed = False

    async def ensure_bucket(self, *, create: bool) -> None:
        del create

    async def presign_upload(
        self, object_key: str, content_type: str, checksum: str | None, expires: int
    ) -> str:
        return f"https://upload.test/{object_key}?type={content_type}&expires={expires}"

    async def head(self, object_key: str) -> dict[str, Any]:
        del object_key
        return self.metadata

    async def presign_download(self, object_key: str, expires: int) -> str:
        return f"https://download.test/{object_key}?expires={expires}"

    async def delete(self, object_key: str) -> None:
        self.deleted.append(object_key)

    async def create_multipart(self, object_key: str, content_type: str) -> str:
        return "provider-upload"

    async def presign_part(
        self, object_key: str, upload_id: str, part_number: int, expires: int
    ) -> str:
        return f"https://part.test/{part_number}?expires={expires}"

    async def list_parts(self, object_key: str, upload_id: str) -> list[dict[str, Any]]:
        return self.parts

    async def complete_multipart(
        self, object_key: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> None:
        self.completed = True

    async def abort_multipart(self, object_key: str, upload_id: str) -> None:
        pass

    async def download_bytes(self, object_key: str, maximum: int) -> bytes:
        return b"text"


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        stored = self.added[-1]
        if isinstance(stored, StoredFile):
            stored.id = "file-id"


class VersionSession(FakeSession):
    async def flush(self) -> None:
        for value in self.added:
            if isinstance(value, FileAsset) and value.id is None:
                value.id = "asset-id"
            if isinstance(value, FileRevision) and value.id is None:
                value.id = "revision-id"
            if isinstance(value, MultipartUpload) and value.id is None:
                value.id = "upload-id"
                value.status = UploadStatus.ACTIVE

    async def scalar(self, _query: object) -> int:
        return 0

    async def get(self, _model: object, _identity: str) -> None:
        return None


@pytest.mark.asyncio
async def test_storage_upload_and_checksum_validation() -> None:
    provider = FakeProvider()
    settings = StorageSettings(
        _env_file=None,
        access_key="access",
        secret_key="secret",
        bucket="test",
        allowed_content_types="text/plain",
    )
    service = StorageService(settings=settings, provider=provider)
    session = FakeSession()
    stored, url = await service.create_upload(  # type: ignore[arg-type]
        session,
        organization_id="org",
        user_id="user",
        filename="../note.txt",
        content_type="text/plain",
        size=4,
        checksum_sha256="YQ==",
    )
    assert stored.original_name == "note.txt"
    assert "upload.test" in url
    await service.complete_upload(stored)
    assert stored.status == FileStatus.AVAILABLE
    assert "download.test" in await service.download_url(stored)
    provider.metadata["ChecksumSHA256"] = "different"
    with pytest.raises(ValueError, match="checksum"):
        await service.complete_upload(stored)


def test_storage_credentials_are_secret() -> None:
    settings = StorageSettings(
        _env_file=None,
        access_key="private-access-value",
        secret_key="private-secret-value",
    )
    assert isinstance(settings.access_key, SecretStr)
    assert "private-access-value" not in repr(settings)
    assert "private-secret-value" not in repr(settings)


@pytest.mark.asyncio
async def test_bucket_lifecycle_and_boto_operations() -> None:
    settings = StorageSettings(
        _env_file=None,
        endpoint_url="http://storage.test",
        access_key="access",
        secret_key="secret",
        bucket="bucket",
    )
    client = Mock()
    client.head_bucket.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "missing"}}, "HeadBucket"
    )
    client.generate_presigned_url.return_value = "https://signed.test"
    client.head_object.return_value = {"ContentLength": 4}
    provider = BotoStorageProvider(settings, client)
    await provider.ensure_bucket(create=True)
    client.create_bucket.assert_called_once_with(Bucket="bucket")
    assert await provider.presign_upload("key", "text/plain", None, 60) == ("https://signed.test")
    assert await provider.presign_download("key", 60) == "https://signed.test"
    assert await provider.head("key") == {"ContentLength": 4}
    await provider.delete("key")
    client.delete_object.assert_called_once_with(Bucket="bucket", Key="key")


@pytest.mark.asyncio
async def test_boto_multipart_operations() -> None:
    settings = StorageSettings(
        _env_file=None, access_key="access", secret_key="secret", bucket="bucket"
    )
    client = Mock()
    client.create_multipart_upload.return_value = {"UploadId": "provider"}
    client.generate_presigned_url.return_value = "https://part.test"
    client.list_parts.return_value = {"Parts": [{"PartNumber": 1, "ETag": '"etag"', "Size": 5}]}
    client.get_object.return_value = {"Body": Mock(read=Mock(return_value=b"data"))}
    provider = BotoStorageProvider(settings, client)
    assert await provider.create_multipart("key", "text/plain") == "provider"
    assert await provider.presign_part("key", "provider", 1, 60) == "https://part.test"
    assert (await provider.list_parts("key", "provider"))[0]["etag"] == "etag"
    await provider.complete_multipart("key", "provider", [{"part_number": 1, "etag": "etag"}])
    await provider.abort_multipart("key", "provider")
    assert await provider.download_bytes("key", 4) == b"data"
    client.complete_multipart_upload.assert_called_once()
    client.abort_multipart_upload.assert_called_once()


@pytest.mark.asyncio
async def test_storage_delete_enqueues_once_and_validates_size() -> None:
    provider = FakeProvider()
    jobs = Mock()
    jobs.enqueue = AsyncMock()
    settings = StorageSettings(
        _env_file=None,
        access_key="access",
        secret_key="secret",
        bucket="test",
        allowed_content_types="text/plain",
    )
    service = StorageService(settings=settings, provider=provider, jobs=jobs)
    stored = StoredFile(
        id="file",
        organization_id="org",
        created_by="user",
        object_key="org/key",
        original_name="note.txt",
        content_type="text/plain",
        expected_size=5,
    )
    with pytest.raises(ValueError, match="size"):
        await service.complete_upload(stored)
    session = Mock()
    await service.delete(session, stored)
    assert stored.status == FileStatus.DELETED
    jobs.enqueue.assert_awaited_once()
    await service.delete(session, stored)
    jobs.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_storage_lifespan_and_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Mock()
    provider.ensure_bucket = AsyncMock()
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.BotoStorageProvider",
        lambda _settings: provider,
    )
    app = SimpleNamespace(state=SimpleNamespace())
    core_settings = SimpleNamespace(production=False)
    async with storage_lifespan(app, core_settings):  # type: ignore[arg-type]
        assert app.state.storage_provider is provider
        checks = storage_health_checks(app)  # type: ignore[arg-type]
        await checks["storage"]()
    assert app.state.storage_provider is None
    assert provider.ensure_bucket.await_count == 2


@pytest.mark.asyncio
async def test_multipart_upload_resumes_and_completes_from_provider_parts() -> None:
    provider = FakeProvider()
    provider.metadata = {"ContentLength": 8, "ETag": "final"}
    jobs = Mock()
    jobs.enqueue = AsyncMock()
    settings = StorageSettings(
        _env_file=None,
        access_key="access",
        secret_key="secret",
        allowed_content_types="text/plain",
        max_file_size=100,
        multipart_threshold=5 * 1024 * 1024,
        multipart_part_size=5 * 1024 * 1024,
    )
    service = StorageService(settings=settings, provider=provider, jobs=jobs)
    session = VersionSession()
    file, revision, _initial_upload, _instructions = await service.create_version_upload(  # type: ignore[arg-type]
        session,
        organization_id="org",
        user_id="user",
        filename="large.txt",
        content_type="text/plain",
        size=8,
    )
    # Force the multipart branch without allocating a multi-megabyte fixture.
    upload = MultipartUpload(
        id="upload-id",
        revision_id=revision.id,
        provider_upload_id="provider-upload",
        part_size=5,
        status=UploadStatus.ACTIVE,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert await service.multipart_parts(revision, upload) == provider.parts
    await service.complete_revision(
        session,
        file,
        revision,
        upload,
        [{"part_number": 1, "etag": "one"}, {"part_number": 2, "etag": "two"}],
    )
    assert provider.completed
    assert revision.status == FileStatus.PROCESSING
    assert file.current_revision_id == revision.id
    jobs.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_version_upload_urls_abort_activation_cdn_and_events() -> None:
    provider = FakeProvider()
    jobs = Mock(enqueue=AsyncMock())
    settings = StorageSettings(
        _env_file=None,
        access_key="access",
        secret_key="secret",
        allowed_content_types="text/plain",
        max_file_size=10 * 1024 * 1024,
        multipart_threshold=5 * 1024 * 1024,
        multipart_part_size=5 * 1024 * 1024,
        cdn_base_url="https://cdn.example",
        cdn_signing_secret="signing",
    )
    service = StorageService(settings=settings, provider=provider, jobs=jobs)
    session = VersionSession()
    file, revision, upload, instructions = await service.create_version_upload(  # type: ignore[arg-type]
        session,
        organization_id="org",
        user_id="user",
        filename="../large.txt",
        content_type="text/plain",
        size=6 * 1024 * 1024,
    )
    assert upload is not None and instructions["mode"] == "multipart"
    assert len(await service.multipart_part_urls(revision, upload, [1, 2])) == 2
    with pytest.raises(ValueError, match="part numbers"):
        await service.multipart_part_urls(revision, upload, [0])
    await service.abort_multipart(revision, upload)
    assert upload.status == UploadStatus.ABORTED

    revision.status = FileStatus.AVAILABLE
    await service.activate_revision(file, revision)
    assert file.current_revision_id == revision.id
    url = await service.revision_download_url(revision, "large.txt")
    assert url.startswith("https://cdn.example/") and "signature=" in url
    with pytest.raises(ValueError, match="available"):
        revision.status = FileStatus.QUARANTINED
        await service.revision_download_url(revision, "large.txt")

    event_session = Mock()
    event_session.scalar = AsyncMock(return_value=None)
    event_session.add = Mock()
    receipt = await service.ingest_event(
        event_session,
        source="test",
        event={"id": "event", "event_type": "ObjectCreated", "object_key": "key"},
    )
    assert receipt.deduplication_key == "event"
    event_session.scalar.return_value = receipt
    assert (
        await service.ingest_event(
            event_session,
            source="test",
            event={"id": "event", "event_type": "ObjectCreated", "object_key": "key"},
        )
        is receipt
    )


def test_content_signature_detection() -> None:
    assert _detected_type(b"\x89PNG\r\n\x1a\nmore") == "image/png"
    assert _detected_type(b"\xff\xd8\xffmore") == "image/jpeg"
    assert _detected_type(b"%PDF-document") == "application/pdf"
    assert _detected_type(b"hello") == "text/plain"
    assert _detected_type(b"\x00\xff") == "application/octet-stream"


def test_media_probe_image_and_video(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    image_path = tmp_path / "image.png"
    Image.new("RGB", (3, 2)).save(image_path)
    settings = StorageSettings(_env_file=None, access_key="a", secret_key="b")
    metadata = _probe_media(image_path.read_bytes(), "image/png", settings)
    assert metadata["width"] == 3 and metadata["height"] == 2

    result = Mock(
        stdout='{"format":{"duration":"2.5"},"streams":[{"codec_name":"h264","width":10,"height":20}]}'
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.subprocess.run", Mock(return_value=result)
    )
    video = _probe_media(b"video", "video/mp4", settings)
    assert video == {
        "duration_seconds": 2.5,
        "width": 10,
        "height": 20,
        "codec": "h264",
    }
    assert _probe_media(b"text", "text/plain", settings) == {}


@pytest.mark.asyncio
async def test_revision_processing_available_and_quarantined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = FileRevision(
        id="revision",
        file_id="asset",
        version=1,
        object_key="org/file/1",
        content_type="text/plain",
        expected_size=4,
        status=FileStatus.PROCESSING,
    )

    class Session:
        commits = 0

        async def get(self, _model: object, _identity: str) -> FileRevision:
            return revision

        async def commit(self) -> None:
            self.commits += 1

    session = Session()

    class Factory:
        @asynccontextmanager
        async def __call__(self):
            yield session

    provider = Mock(download_bytes=AsyncMock(return_value=b"text"))
    engine = Mock(dispose=AsyncMock())
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.Settings",
        lambda: Mock(database_url="postgresql+asyncpg://unused"),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.get_storage_settings",
        lambda: StorageSettings(_env_file=None, access_key="a", secret_key="b"),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.create_async_engine",
        lambda *_a, **_k: engine,
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.async_sessionmaker",
        lambda *_a, **_k: Factory(),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.BotoStorageProvider", lambda _settings: provider
    )

    result = await process_revision_handler("job", {"revision_id": revision.id})
    assert result == {"status": FileStatus.AVAILABLE, "content_type": "text/plain"}
    revision.status = FileStatus.PROCESSING
    revision.content_type = "image/png"
    result = await process_revision_handler("job", {"revision_id": revision.id})
    assert result["status"] == FileStatus.QUARANTINED
    assert "does not match" in (revision.quarantine_reason or "")

    revision.status = FileStatus.PROCESSING
    revision.content_type = "image/png"
    provider.download_bytes.return_value = b"\x89PNG\r\n\x1a\ncorrupt"
    result = await process_revision_handler("job", {"revision_id": revision.id})
    assert result["status"] == FileStatus.QUARANTINED
    assert revision.quarantine_reason
    assert session.commits == 3
    assert engine.dispose.await_count == 3


@pytest.mark.asyncio
async def test_storage_lifecycle_expires_uploads_and_old_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = datetime.now(UTC) - timedelta(days=10)
    revision = FileRevision(
        id="revision",
        file_id="asset",
        version=1,
        object_key="org/file/1",
        content_type="text/plain",
        expected_size=4,
        status=FileStatus.AVAILABLE,
        created_at=old,
    )
    upload = MultipartUpload(
        id="upload",
        revision_id=revision.id,
        provider_upload_id="provider-upload",
        part_size=5,
        status=UploadStatus.ACTIVE,
        expires_at=old,
    )
    asset = FileAsset(
        id="asset",
        organization_id="org",
        created_by="user",
        original_name="file.txt",
        current_revision_id="new-revision",
        noncurrent_retention_days=1,
    )

    class Result:
        def __init__(self, values: list[object]) -> None:
            self.values = values

        def all(self) -> list[object]:
            return self.values

    class Session:
        def __init__(self) -> None:
            self.results = iter(([upload], [asset], [revision]))
            self.committed = False

        async def scalars(self, _query: object) -> Result:
            return Result(list(next(self.results)))

        async def get(self, _model: object, _identity: str) -> FileRevision:
            return revision

        async def commit(self) -> None:
            self.committed = True

    session = Session()

    class Factory:
        @asynccontextmanager
        async def __call__(self):
            yield session

    provider = Mock(abort_multipart=AsyncMock(), delete=AsyncMock())
    engine = Mock(dispose=AsyncMock())
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.Settings",
        lambda: Mock(database_url="postgresql+asyncpg://unused"),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.get_storage_settings",
        lambda: StorageSettings(_env_file=None, access_key="a", secret_key="b"),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.create_async_engine",
        lambda *_a, **_k: engine,
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.async_sessionmaker",
        lambda *_a, **_k: Factory(),
    )
    monkeypatch.setattr(
        "backend_foundation.modules.storage.service.BotoStorageProvider", lambda _settings: provider
    )

    assert await storage_lifecycle_handler("job", {}) == {"cleaned": 2}
    assert upload.status == UploadStatus.EXPIRED
    assert revision.status == FileStatus.DELETED
    provider.abort_multipart.assert_awaited_once()
    provider.delete.assert_awaited_once_with(revision.object_key)
    assert session.committed
    engine.dispose.assert_awaited_once()
