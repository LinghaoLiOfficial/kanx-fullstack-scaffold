from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from backend_foundation.modules.auth.dependencies import AuthPrincipal
from backend_foundation.modules.rbac.service import AuthorizationError
from backend_foundation.modules.storage.models import FileStatus, StoredFile
from backend_foundation.modules.storage.router import (
    UploadRequest,
    complete_upload,
    create_upload,
    delete_file,
    download,
    file_metadata,
)


def stored_file() -> StoredFile:
    return StoredFile(
        id="file",
        organization_id="org",
        created_by="user",
        object_key="org/key",
        original_name="note.txt",
        content_type="text/plain",
        expected_size=4,
        actual_size=4,
        status=FileStatus.AVAILABLE,
        created_at=datetime.now(UTC),
    )


class Session:
    def __init__(self, value: StoredFile | None) -> None:
        self.value = value
        self.commit = AsyncMock()

    async def scalar(self, _query: object) -> StoredFile | None:
        return self.value


@pytest.mark.asyncio
async def test_storage_routes_cover_authorized_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = stored_file()
    service = Mock()
    service.settings = SimpleNamespace(presign_seconds=900)
    service.create_upload = AsyncMock(return_value=(stored, "https://upload.test"))
    service.complete_upload = AsyncMock(return_value=stored)
    service.download_url = AsyncMock(return_value="https://download.test")
    service.delete = AsyncMock()
    monkeypatch.setattr("backend_foundation.modules.storage.router.StorageService", lambda: service)
    require = AsyncMock()
    monkeypatch.setattr("backend_foundation.modules.storage.router.RoleService.require", require)
    principal = AuthPrincipal("user", "person@example.test", "Person", datetime.now(UTC))
    session = Session(stored)
    created = await create_upload(  # type: ignore[arg-type]
        "org",
        UploadRequest(filename="note.txt", content_type="text/plain", size=4),
        principal,
        session,
    )
    assert created["upload_url"] == "https://upload.test"
    assert (await complete_upload("org", "file", principal, session))["status"] == (  # type: ignore[arg-type]
        FileStatus.AVAILABLE
    )
    metadata = await file_metadata("org", "file", principal, session)  # type: ignore[arg-type]
    assert metadata["filename"] == "note.txt"
    assert (await download("org", "file", principal, session))["download_url"] == (  # type: ignore[arg-type]
        "https://download.test"
    )
    await delete_file("org", "file", principal, session)  # type: ignore[arg-type]
    service.delete.assert_awaited_once()
    assert require.await_count == 5


@pytest.mark.asyncio
async def test_storage_routes_hide_cross_org_and_missing_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = AuthPrincipal("user", "person@example.test", "Person", datetime.now(UTC))
    deny = AsyncMock(side_effect=AuthorizationError("denied"))
    monkeypatch.setattr("backend_foundation.modules.storage.router.RoleService.require", deny)
    with pytest.raises(HTTPException) as denied:
        await file_metadata("other", "file", principal, Session(stored_file()))  # type: ignore[arg-type]
    assert denied.value.status_code == 403

    monkeypatch.setattr(
        "backend_foundation.modules.storage.router.RoleService.require", AsyncMock()
    )
    with pytest.raises(HTTPException) as missing:
        await file_metadata("org", "missing", principal, Session(None))  # type: ignore[arg-type]
    assert missing.value.status_code == 404
