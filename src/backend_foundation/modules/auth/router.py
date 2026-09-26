from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.audit import record_audit
from ..database.session import get_session
from ..rbac.models import Organization, OrganizationMembership
from ..rbac.service import RoleService
from ..users.service import UserService
from .dependencies import AuthPrincipal, current_user
from .service import AuthenticationError, AuthService, RefreshTokenReuseError, digest
from .settings import get_auth_settings

router = APIRouter(tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class EmailRequest(BaseModel):
    email: EmailStr


class TokenRequest(BaseModel):
    token: str


class ResetRequest(TokenRequest):
    password: str = Field(min_length=12, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=256)


def _set_session_cookies(response: Response, refresh: str, csrf: str) -> None:
    settings = get_auth_settings()
    max_age = settings.refresh_token_days * 86400
    response.set_cookie(
        "refresh_token",
        refresh,
        max_age=max_age,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/auth",
    )
    response.set_cookie(
        "csrf_token",
        csrf,
        max_age=max_age,
        httponly=False,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def _validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    allowed = {item.strip() for item in get_auth_settings().allowed_origins.split(",")}
    if not origin or origin not in allowed:
        raise HTTPException(status_code=403, detail="Origin is not allowed")


@router.post("/auth/register", status_code=202)
async def register(
    body: RegisterRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, str]:
    try:
        await AuthService().register(
            session, email=str(body.email), password=body.password, display_name=body.display_name
        )
        await record_audit(session, "auth.register", details={"email": str(body.email)})
        await session.commit()
    except AuthenticationError:
        await session.rollback()
    return {"message": "If registration is available, a verification email will be sent."}


@router.post("/auth/verify-email")
async def verify_email(
    body: TokenRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, str]:
    try:
        await AuthService().verify_email(session, body.token)
        await session.commit()
    except AuthenticationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"message": "Email verified"}


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str | int]:
    try:
        tokens = await AuthService().login(session, str(body.email), body.password)
        await record_audit(session, "auth.login", details={"email": str(body.email)})
        await session.commit()
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    _set_session_cookies(response, tokens.refresh_token, tokens.csrf_token)
    return {
        "access_token": tokens.access_token,
        "token_type": "bearer",
        "expires_in": tokens.expires_in,
    }


@router.post("/auth/refresh")
async def refresh(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    refresh_token: Annotated[str | None, Cookie()] = None,
    csrf_cookie: Annotated[str | None, Cookie(alias="csrf_token")] = None,
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> dict[str, str | int]:
    _validate_origin(request)
    if not refresh_token or not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    try:
        tokens = await AuthService().rotate(session, refresh_token, csrf_header)
        await session.commit()
    except RefreshTokenReuseError as error:
        await session.commit()
        raise HTTPException(status_code=401, detail="Refresh session revoked") from error
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    _set_session_cookies(response, tokens.refresh_token, tokens.csrf_token)
    return {
        "access_token": tokens.access_token,
        "token_type": "bearer",
        "expires_in": tokens.expires_in,
    }


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    refresh_token: Annotated[str | None, Cookie()] = None,
    csrf_cookie: Annotated[str | None, Cookie(alias="csrf_token")] = None,
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    _validate_origin(request)
    if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    if refresh_token:
        from .models import AuthSession

        current = await session.scalar(
            select(AuthSession).where(AuthSession.refresh_digest == digest(refresh_token))
        )
        if current is not None:
            await AuthService().revoke_family(session, current.family_id)
            await session.commit()
    response.delete_cookie("refresh_token", path="/auth")
    response.delete_cookie("csrf_token", path="/")


@router.post("/auth/forgot-password", status_code=202)
async def forgot_password(
    body: EmailRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, str]:
    await AuthService().forgot_password(session, str(body.email))
    await session.commit()
    return {"message": "If the account exists, a reset email will be sent."}


@router.post("/auth/resend-verification", status_code=202)
async def resend_verification(
    body: EmailRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, str]:
    service = AuthService()
    user = await service.users.by_email(session, str(body.email))
    if user is not None and user.email_verified_at is None:
        from .models import AuthTokenType

        token = await service._issue_one_time_token(session, user, AuthTokenType.VERIFY_EMAIL)
        await service.email.enqueue(
            session,
            to=user.email,
            template="verify_email",
            context={"url": f"{service.email_url}/verify-email?token={token}"},
            idempotency_key=f"verify:{user.id}:{digest(token)}",
        )
        await session.commit()
    return {"message": "If the account exists, a verification email will be sent."}


@router.post("/auth/reset-password")
async def reset_password(
    body: ResetRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, str]:
    try:
        await AuthService().reset_password(session, body.token, body.password)
        await session.commit()
    except AuthenticationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"message": "Password reset"}


@router.post("/auth/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user: Annotated[AuthPrincipal, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    from .service import password_hash

    if len(body.new_password) < 12:
        raise HTTPException(status_code=400, detail="Password is too short")
    account = await AuthService().users.by_id(session, user.id)
    if account is None:
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    if not password_hash.verify(body.current_password, account.password_hash):
        raise HTTPException(status_code=400, detail="Current password is invalid")
    UserService.change_password(account, password_hash.hash(body.new_password))
    service = AuthService()
    await service.revoke_user_sessions(session, user.id)
    await service.email.enqueue(
        session,
        to=account.email,
        template="security_notice",
        context={"message": "Your password was changed."},
        idempotency_key=f"password-change:{user.id}:{account.token_version}",
    )
    await session.commit()
    return {"message": "Password changed"}


@router.get("/users/me")
async def me(
    user: Annotated[AuthPrincipal, Depends(current_user)],
) -> dict[str, str | bool]:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "email_verified": user.email_verified,
    }


@router.get("/users/me/context")
async def user_context(
    user: Annotated[AuthPrincipal, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    organizations = (
        await session.scalars(
            select(Organization)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id == Organization.id,
            )
            .where(OrganizationMembership.user_id == user.id)
            .order_by(Organization.name)
        )
    ).all()
    roles = RoleService()
    return {
        "user": await me(user),
        "organizations": [
            {
                "id": organization.id,
                "name": organization.name,
                "slug": organization.slug,
                "permissions": sorted(await roles.permissions(session, user.id, organization.id)),
            }
            for organization in organizations
        ],
    }
