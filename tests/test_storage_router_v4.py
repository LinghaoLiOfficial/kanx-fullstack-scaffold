import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend_foundation.modules.auth.dependencies import AuthPrincipal
from backend_foundation.modules.storage.models import (
    FileAsset,
    FileRevision,
    FileStatus,
    MultipartUpload,
    UploadStatus,
)
from backend_foundation.modules.storage.router import (
    CompleteMultipartRequest,
    PartUrlsRequest,
    UploadRequest,
    _authorized_asset,
    _normalized_events,
    _revision_upload,
    abort_upload,
    activate_version,
    complete_revision,
    consume_s3_event,
    create_file_version,
    create_upload_session,
    download_asset,
    list_versions,
    multipart_part_urls,
    uploaded_parts,
)


class Result:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class Session:
    def __init__(self, asset, revision, upload) -> None:
        self.asset = asset
        self.revision = revision
        self.upload = upload

    async def scalar(self, query):
        entity = query.column_descriptions[0].get("entity")
        if entity is FileRevision:
            return self.revision
        return self.asset

    async def scalars(self, _query):
        return Result([self.revision])

    async def get(self, model, identity):
        values = {
            (FileRevision, self.revision.id): self.revision,
            (MultipartUpload, self.upload.id): self.upload,
        }
        return values.get((model, identity))

    async def commit(self):
        pass


def objects():
    asset = FileAsset(
        id="file",
        organization_id="org",
        created_by="user",
        original_name="file.txt",
        current_revision_id="revision",
    )
    revision = FileRevision(
        id="revision",
        file_id=asset.id,
        version=1,
        object_key="org/file/v1",
        content_type="text/plain",
        expected_size=4,
        actual_size=4,
        status=FileStatus.AVAILABLE,
        created_at=datetime.now(UTC),
    )
    upload = MultipartUpload(
        id="upload",
        revision_id=revision.id,
        provider_upload_id="provider",
        part_size=5,
        status=UploadStatus.ACTIVE,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return asset, revision, upload


def user():
    return AuthPrincipal("user", "u@example.com", "User", datetime.now(UTC))


@pytest.mark.asyncio
async def test_v4_storage_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    asset, revision, upload = objects()
    session = Session(asset, revision, upload)
    monkeypatch.setattr(
        "backend_foundation.modules.storage.router.RoleService.require", AsyncMock()
    )
    assert await _authorized_asset(session, "org", asset.id, user(), "files:read") is asset  # type: ignore[arg-type]
    assert await _revision_upload(session, asset.id, upload.id) == (revision, upload)  # type: ignore[arg-type]
    records = _normalized_events(
        {
            "Records": [
                {
                    "eventName": "ObjectCreated:Put",
                    "responseElements": {"x-amz-request-id": "request"},
                    "s3": {"object": {"key": "folder%2Ffile+name", "sequencer": "sequence"}},
                }
            ]
        }
    )
    assert records[0]["object_key"] == "folder/file name"
    assert _normalized_events({"event_type": "plain"})[0]["event_type"] == "plain"


@pytest.mark.asyncio
async def test_v4_storage_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    asset, revision, upload = objects()
    session = Session(asset, revision, upload)
    service = Mock()
    service.settings = SimpleNamespace(presign_seconds=900, event_webhook_secret=Mock())
    service.create_version_upload = AsyncMock(
        return_value=(asset, revision, upload, {"mode": "multipart"})
    )
    service.multipart_part_urls = AsyncMock(
        return_value=[{"part_number": 1, "upload_url": "https://upload"}]
    )
    service.multipart_parts = AsyncMock(return_value=[{"part_number": 1, "etag": "etag"}])
    service.complete_revision = AsyncMock(return_value=revision)
    service.abort_multipart = AsyncMock()
    service.activate_revision = AsyncMock()
    service.revision_download_url = AsyncMock(return_value="https://download")
    monkeypatch.setattr("backend_foundation.modules.storage.router.StorageService", lambda: service)
    monkeypatch.setattr(
        "backend_foundation.modules.storage.router.RoleService.require", AsyncMock()
    )
    body = UploadRequest(filename="file.txt", content_type="text/plain", size=4)
    created = await create_upload_session("org", body, user(), session)  # type: ignore[arg-type]
    assert created["file_id"] == asset.id
    version = await create_file_version("org", asset.id, body, user(), session)  # type: ignore[arg-type]
    assert version["revision_id"] == revision.id
    urls = await multipart_part_urls(
        "org",
        asset.id,
        upload.id,
        PartUrlsRequest(part_numbers=[1]),
        user(),
        session,  # type: ignore[arg-type]
    )
    assert urls["parts"][0]["part_number"] == 1
    parts = await uploaded_parts("org", asset.id, upload.id, user(), session)  # type: ignore[arg-type]
    assert parts["parts"][0]["etag"] == "etag"
    completed = await complete_revision(
        "org",
        asset.id,
        revision.id,
        CompleteMultipartRequest(parts=[]),
        user(),
        session,  # type: ignore[arg-type]
    )
    assert completed["status"] == FileStatus.AVAILABLE
    await abort_upload("org", asset.id, upload.id, user(), session)  # type: ignore[arg-type]
    versions = await list_versions("org", asset.id, user(), session)  # type: ignore[arg-type]
    assert versions[0]["current"] is True
    activated = await activate_version("org", asset.id, revision.id, user(), session)  # type: ignore[arg-type]
    assert activated["current_revision_id"] == revision.id
    downloaded = await download_asset("org", asset.id, user(), session)  # type: ignore[arg-type]
    assert downloaded["download_url"] == "https://download"


@pytest.mark.asyncio
async def test_signed_storage_event_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "event-secret"
    body = json.dumps({"event_type": "ObjectCreated", "object_key": "key", "id": "event"}).encode()
    service = Mock()
    service.settings = SimpleNamespace(event_webhook_secret=Mock())
    service.settings.event_webhook_secret.get_secret_value.return_value = secret
    service.ingest_event = AsyncMock()
    monkeypatch.setattr("backend_foundation.modules.storage.router.StorageService", lambda: service)
    request = Mock(body=AsyncMock(return_value=body))
    session = Mock(commit=AsyncMock())
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    result = await consume_s3_event(request, session, signature)  # type: ignore[arg-type]
    assert result == {"accepted": 1}
