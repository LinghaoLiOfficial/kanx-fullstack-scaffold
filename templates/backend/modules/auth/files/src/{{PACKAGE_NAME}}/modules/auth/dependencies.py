from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.request_context import set_business_context
from ..database.session import get_session
from ..rbac.service import AuthorizationError, RoleService
from ..users.service import UserService
from .models import AuthSession
from .settings import get_auth_settings

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthPrincipal:
    id: str
    email: str
    display_name: str
    email_verified_at: datetime | None

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthPrincipal:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    settings = get_auth_settings()
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="Invalid access token") from error
    users = UserService()
    user = await users.by_id(session, str(claims["sub"]))
    auth_session = await session.get(AuthSession, str(claims["sid"]))
    if (
        user is None
        or not users.active(user)
        or user.token_version != int(claims["ver"])
        or auth_session is None
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    set_business_context(user.id)
    return AuthPrincipal(user.id, user.email, user.display_name, user.email_verified_at)


async def require_verified_user(
    user: Annotated[AuthPrincipal, Depends(current_user)],
) -> AuthPrincipal:
    if user.email_verified_at is None:
        raise HTTPException(status_code=403, detail="Verified email required")
    return user


def require_permission(
    permission: str,
) -> Callable[..., object]:
    async def dependency(
        organization_id: str,
        user: Annotated[AuthPrincipal, Depends(require_verified_user)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> AuthPrincipal:
        try:
            await RoleService().require(session, user.id, organization_id, permission)
        except AuthorizationError as error:
            raise HTTPException(status_code=403, detail="Permission denied") from error
        set_business_context(user.id, organization_id)
        return user

    return dependency
