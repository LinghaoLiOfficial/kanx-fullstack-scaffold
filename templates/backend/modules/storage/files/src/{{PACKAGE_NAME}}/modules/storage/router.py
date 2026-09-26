from __future__ import annotations

import hashlib
import hmac
import json
from typing import Annotated, Any
from urllib.parse import unquote_plus

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.audit import record_audit
from ..auth.dependencies import AuthPrincipal, require_verified_user
from ..database.session import get_session
from ..rbac.service import AuthorizationError, RoleService
from .models import FileAsset, FileRevision, FileStatus, MultipartUpload, StoredFile
from .service import StorageService

router = APIRouter(prefix="/organizations/{organization_id}/files", tags=["storage"])
events_router = APIRouter(prefix="/storage/events", tags=["storage-events"])


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    checksum_sha256: str | None = Field(default=None, pattern=r"^[A-Za-z0-9+/]{43}=$")


class PartUrlsRequest(BaseModel):
    part_numbers: list[int] = Field(min_length=1, max_length=100)


class CompleteMultipartRequest(BaseModel):
    parts: list[dict[str, str | int]] = Field(default_factory=list, max_length=10000)


async def _authorized_asset(
    session: AsyncSession,
    organization_id: str,
    file_id: str,
    user: AuthPrincipal,
    permission: str,
) -> FileAsset:
    try:
        await RoleService().require(session, user.id, organization_id, permission)
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    file = await session.scalar(
        select(FileAsset).where(
            FileAsset.id == file_id,
            FileAsset.organization_id == organization_id,
            FileAsset.deleted_at.is_(None),
        )
    )
    if file is None:
        raise HTTPException(status_code=404, detail="File not found")
    return file


async def _revision_upload(
    session: AsyncSession, file_id: str, upload_id: str
) -> tuple[FileRevision, MultipartUpload]:
    upload = await session.get(MultipartUpload, upload_id)
    revision = await session.get(FileRevision, upload.revision_id) if upload else None
    if upload is None or revision is None or revision.file_id != file_id:
        raise HTTPException(status_code=404, detail="Upload session not found")
    return revision, upload


@router.post("/upload-sessions", status_code=201)
async def create_upload_session(
    organization_id: str,
    body: UploadRequest,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    try:
        await RoleService().require(session, user.id, organization_id, "files:uploads:write")
        file, revision, _upload, instructions = await StorageService().create_version_upload(
            session,
            organization_id=organization_id,
            user_id=user.id,
            filename=body.filename,
            content_type=body.content_type,
            size=body.size,
            checksum_sha256=body.checksum_sha256,
        )
        await session.commit()
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "file_id": file.id,
        "revision_id": revision.id,
        "version": revision.version,
        "expires_in": StorageService().settings.presign_seconds,
        **instructions,
    }


@router.post("/{file_id}/versions", status_code=201)
async def create_file_version(
    organization_id: str,
    file_id: str,
    body: UploadRequest,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    file = await _authorized_asset(session, organization_id, file_id, user, "files:versions:write")
    try:
        _, revision, _, instructions = await StorageService().create_version_upload(
            session,
            organization_id=organization_id,
            user_id=user.id,
            filename=body.filename,
            content_type=body.content_type,
            size=body.size,
            checksum_sha256=body.checksum_sha256,
            file=file,
        )
        await session.commit()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"file_id": file.id, "revision_id": revision.id, **instructions}


@router.post("/{file_id}/uploads/{upload_id}/part-urls")
async def multipart_part_urls(
    organization_id: str,
    file_id: str,
    upload_id: str,
    body: PartUrlsRequest,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _authorized_asset(session, organization_id, file_id, user, "files:uploads:write")
    revision, upload = await _revision_upload(session, file_id, upload_id)
    try:
        parts = await StorageService().multipart_part_urls(revision, upload, body.part_numbers)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"parts": parts, "expires_in": StorageService().settings.presign_seconds}


@router.get("/{file_id}/uploads/{upload_id}/parts")
async def uploaded_parts(
    organization_id: str,
    file_id: str,
    upload_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _authorized_asset(session, organization_id, file_id, user, "files:uploads:write")
    revision, upload = await _revision_upload(session, file_id, upload_id)
    return {"parts": await StorageService().multipart_parts(revision, upload)}


@router.post("/{file_id}/revisions/{revision_id}/complete")
async def complete_revision(
    organization_id: str,
    file_id: str,
    revision_id: str,
    body: CompleteMultipartRequest,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    file = await _authorized_asset(session, organization_id, file_id, user, "files:uploads:write")
    revision = await session.scalar(
        select(FileRevision).where(FileRevision.id == revision_id).with_for_update()
    )
    if revision is None or revision.file_id != file.id:
        raise HTTPException(status_code=404, detail="Revision not found")
    upload = await session.scalar(
        select(MultipartUpload).where(MultipartUpload.revision_id == revision.id)
    )
    try:
        await StorageService().complete_revision(session, file, revision, upload, body.parts)
        await session.commit()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"file_id": file.id, "revision_id": revision.id, "status": revision.status}


@router.delete("/{file_id}/uploads/{upload_id}", status_code=204)
async def abort_upload(
    organization_id: str,
    file_id: str,
    upload_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    await _authorized_asset(session, organization_id, file_id, user, "files:uploads:write")
    revision, upload = await _revision_upload(session, file_id, upload_id)
    await StorageService().abort_multipart(revision, upload)
    await session.commit()


@router.get("/{file_id}/versions")
async def list_versions(
    organization_id: str,
    file_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    file = await _authorized_asset(session, organization_id, file_id, user, "files:versions:read")
    revisions = (
        await session.scalars(
            select(FileRevision)
            .where(FileRevision.file_id == file.id)
            .order_by(FileRevision.version.desc())
        )
    ).all()
    return [
        {
            "id": item.id,
            "version": item.version,
            "status": item.status,
            "content_type": item.detected_content_type or item.content_type,
            "size": item.actual_size,
            "current": item.id == file.current_revision_id,
            "metadata": item.media_metadata,
        }
        for item in revisions
    ]


@router.post("/{file_id}/versions/{revision_id}/activate")
async def activate_version(
    organization_id: str,
    file_id: str,
    revision_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    file = await _authorized_asset(session, organization_id, file_id, user, "files:versions:write")
    revision = await session.get(FileRevision, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    try:
        await StorageService().activate_revision(file, revision)
        await session.commit()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"file_id": file.id, "current_revision_id": revision.id}


@router.post("/assets/{file_id}/download")
async def download_asset(
    organization_id: str,
    file_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    file = await _authorized_asset(session, organization_id, file_id, user, "files:read")
    revision = (
        await session.get(FileRevision, file.current_revision_id)
        if file.current_revision_id
        else None
    )
    if revision is None:
        raise HTTPException(status_code=409, detail="File has no current revision")
    if revision.status == FileStatus.QUARANTINED:
        try:
            await RoleService().require(session, user.id, organization_id, "files:quarantine:read")
        except AuthorizationError as error:
            raise HTTPException(status_code=403, detail="Permission denied") from error
    try:
        url = await StorageService().revision_download_url(revision, file.original_name)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"download_url": url, "expires_in": StorageService().settings.presign_seconds}


def _normalized_events(value: dict[str, Any]) -> list[dict[str, Any]]:
    if "Records" not in value:
        return [value]
    result = []
    for record in value["Records"]:
        item = record.get("s3", {}).get("object", {})
        result.append(
            {
                "id": record.get("responseElements", {}).get("x-amz-request-id"),
                "sequencer": item.get("sequencer"),
                "event_type": record.get("eventName", "unknown"),
                "object_key": unquote_plus(str(item.get("key", ""))),
                "raw": record,
            }
        )
    return result


@events_router.post("/s3", status_code=202)
async def consume_s3_event(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    signature: Annotated[str | None, Header(alias="X-Storage-Signature")] = None,
) -> dict[str, int]:
    body = await request.body()
    secret = StorageService().settings.event_webhook_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=503, detail="Storage event webhook is disabled")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if signature is None or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid event signature")
    try:
        value = json.loads(body)
        events = _normalized_events(value)
        service = StorageService()
        for event in events:
            await service.ingest_event(session, source="http", event=event)
        await session.commit()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid storage event") from error
    return {"accepted": len(events)}


async def _authorized_file(
    session: AsyncSession,
    organization_id: str,
    file_id: str,
    user: AuthPrincipal,
    permission: str,
) -> StoredFile:
    try:
        await RoleService().require(session, user.id, organization_id, permission)
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    stored = await session.scalar(
        select(StoredFile).where(
            StoredFile.id == file_id,
            StoredFile.organization_id == organization_id,
        )
    )
    if stored is None:
        raise HTTPException(status_code=404, detail="File not found")
    return stored


@router.post("/uploads", status_code=201)
async def create_upload(
    organization_id: str,
    body: UploadRequest,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str | int]:
    try:
        await RoleService().require(session, user.id, organization_id, "files:write")
        stored, url = await StorageService().create_upload(
            session,
            organization_id=organization_id,
            user_id=user.id,
            filename=body.filename,
            content_type=body.content_type,
            size=body.size,
            checksum_sha256=body.checksum_sha256,
        )
        await record_audit(
            session,
            "storage.upload.create",
            resource_type="file",
            resource_id=stored.id,
            details={
                "filename": stored.original_name,
                "size": body.size,
                "content_type": body.content_type,
            },
        )
        await session.commit()
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "id": stored.id,
        "upload_url": url,
        "expires_in": StorageService().settings.presign_seconds,
    }


@router.post("/{file_id}/complete")
async def complete_upload(
    organization_id: str,
    file_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    stored = await _authorized_file(session, organization_id, file_id, user, "files:write")
    try:
        await StorageService().complete_upload(stored)
        await record_audit(
            session, "storage.upload.complete", resource_type="file", resource_id=stored.id
        )
        await session.commit()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"id": stored.id, "status": stored.status}


@router.get("/{file_id}")
async def file_metadata(
    organization_id: str,
    file_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str | int | None]:
    stored = await _authorized_file(session, organization_id, file_id, user, "files:read")
    return {
        "id": stored.id,
        "filename": stored.original_name,
        "content_type": stored.content_type,
        "size": stored.actual_size,
        "status": stored.status,
    }


@router.post("/{file_id}/download")
async def download(
    organization_id: str,
    file_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str | int]:
    stored = await _authorized_file(session, organization_id, file_id, user, "files:read")
    try:
        url = await StorageService().download_url(stored)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"download_url": url, "expires_in": StorageService().settings.presign_seconds}


@router.delete("/{file_id}", status_code=204)
async def delete_file(
    organization_id: str,
    file_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    stored = await _authorized_file(session, organization_id, file_id, user, "files:delete")
    await StorageService().delete(session, stored)
    await record_audit(session, "storage.file.delete", resource_type="file", resource_id=stored.id)
    await session.commit()
