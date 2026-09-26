from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from pwdlib import PasswordHash
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..email.service import EmailService
from ..rbac.service import OrganizationService
from ..users.service import DuplicateUserError, UserAccount, UserService
from .models import AuthSession, AuthToken, AuthTokenType
from .settings import AuthSettings, get_auth_settings

password_hash = PasswordHash.recommended()


class AuthenticationError(ValueError):
    pass


class RefreshTokenReuseError(AuthenticationError):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    csrf_token: str
    expires_in: int


class AuthService:
    def __init__(
        self,
        settings: AuthSettings | None = None,
        users: UserService | None = None,
        email: EmailService | None = None,
        organizations: OrganizationService | None = None,
    ) -> None:
        self.settings = settings or get_auth_settings()
        self.users = users or UserService()
        self.email = email or EmailService()
        self.organizations = organizations or OrganizationService()

    async def register(
        self, session: AsyncSession, *, email: str, password: str, display_name: str
    ) -> UserAccount:
        if len(password) < 12:
            raise AuthenticationError("Password must contain at least 12 characters")
        try:
            user = await self.users.create(
                session,
                email=email,
                password_hash=password_hash.hash(password),
                display_name=display_name,
            )
        except DuplicateUserError as error:
            raise AuthenticationError("Unable to register account") from error
        organization_id = await self.organizations.create_personal(
            session, user_id=user.id, display_name=user.display_name
        )
        token = await self._issue_one_time_token(session, user, AuthTokenType.VERIFY_EMAIL)
        await self.email.enqueue(
            session,
            to=user.email,
            template="verify_email",
            context={"url": f"{self.email_url}/verify-email?token={token}"},
            idempotency_key=f"verify:{user.id}:{digest(token)}",
            organization_id=organization_id,
        )
        return user

    @property
    def email_url(self) -> str:
        from ..email.settings import get_email_settings

        return get_email_settings().public_base_url.rstrip("/")

    async def login(self, session: AsyncSession, email: str, password: str) -> TokenPair:
        user = await self.users.by_email(session, email)
        if user is None or not password_hash.verify(password, user.password_hash):
            raise AuthenticationError("Invalid credentials")
        if not self.users.active(user) or user.email_verified_at is None:
            raise AuthenticationError("Email verification is required")
        return await self._new_session(session, user)

    async def verify_email(self, session: AsyncSession, raw_token: str) -> UserAccount:
        user = await self._consume_token(session, raw_token, AuthTokenType.VERIFY_EMAIL)
        self.users.activate(user, datetime.now(UTC))
        return user

    async def forgot_password(self, session: AsyncSession, email: str) -> None:
        user = await self.users.by_email(session, email)
        if user is None:
            return
        token = await self._issue_one_time_token(session, user, AuthTokenType.RESET_PASSWORD)
        await self.email.enqueue(
            session,
            to=user.email,
            template="reset_password",
            context={"url": f"{self.email_url}/reset-password?token={token}"},
            idempotency_key=f"reset:{user.id}:{digest(token)}",
        )

    async def reset_password(self, session: AsyncSession, token: str, password: str) -> UserAccount:
        if len(password) < 12:
            raise AuthenticationError("Password must contain at least 12 characters")
        user = await self._consume_token(session, token, AuthTokenType.RESET_PASSWORD)
        self.users.change_password(user, password_hash.hash(password))
        await self.revoke_user_sessions(session, user.id)
        await self.email.enqueue(
            session,
            to=user.email,
            template="security_notice",
            context={"message": "Your password was reset."},
            idempotency_key=f"password-reset:{user.id}:{user.token_version}",
        )
        return user

    async def rotate(self, session: AsyncSession, raw_token: str, csrf_token: str) -> TokenPair:
        current = await session.scalar(
            select(AuthSession)
            .where(AuthSession.refresh_digest == digest(raw_token))
            .with_for_update()
        )
        if current is None:
            raise AuthenticationError("Invalid refresh token")
        if current.rotated_at is not None:
            await self.revoke_family(session, current.family_id)
            raise RefreshTokenReuseError("Refresh token reuse detected")
        if current.revoked_at is not None or current.expires_at <= datetime.now(UTC):
            raise AuthenticationError("Refresh token expired or revoked")
        if not secrets.compare_digest(current.csrf_digest, digest(csrf_token)):
            raise AuthenticationError("Invalid CSRF token")
        user = await self.users.by_id(session, current.user_id)
        if user is None or not self.users.active(user):
            raise AuthenticationError("User is not active")
        current.rotated_at = datetime.now(UTC)
        return await self._new_session(session, user, family_id=current.family_id)

    async def revoke_family(self, session: AsyncSession, family_id: str) -> None:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def revoke_user_sessions(self, session: AsyncSession, user_id: str) -> None:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def disable_user(self, session: AsyncSession, user: UserAccount) -> None:
        self.users.disable(user)
        await self.revoke_user_sessions(session, user.id)

    async def _new_session(
        self, session: AsyncSession, user: UserAccount, family_id: str | None = None
    ) -> TokenPair:
        refresh = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        auth_session = AuthSession(
            user_id=user.id,
            family_id=family_id or str(uuid4()),
            refresh_digest=digest(refresh),
            csrf_digest=digest(csrf),
            expires_at=datetime.now(UTC) + timedelta(days=self.settings.refresh_token_days),
        )
        session.add(auth_session)
        await session.flush()
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=self.settings.access_token_minutes)
        access = jwt.encode(
            {
                "sub": user.id,
                "sid": auth_session.id,
                "ver": user.token_version,
                "iss": self.settings.jwt_issuer,
                "aud": self.settings.jwt_audience,
                "iat": now,
                "exp": expires,
            },
            self.settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )
        return TokenPair(access, refresh, csrf, self.settings.access_token_minutes * 60)

    async def _issue_one_time_token(
        self, session: AsyncSession, user: UserAccount, token_type: AuthTokenType
    ) -> str:
        raw = secrets.token_urlsafe(48)
        session.add(
            AuthToken(
                user_id=user.id,
                token_type=token_type,
                digest=digest(raw),
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
        )
        return raw

    async def _consume_token(
        self, session: AsyncSession, raw_token: str, token_type: AuthTokenType
    ) -> UserAccount:
        token = await session.scalar(
            select(AuthToken)
            .where(AuthToken.digest == digest(raw_token), AuthToken.token_type == token_type)
            .with_for_update()
        )
        if token is None or token.consumed_at is not None or token.expires_at <= datetime.now(UTC):
            raise AuthenticationError("Invalid or expired token")
        user = await self.users.by_id(session, token.user_id)
        if user is None:
            raise AuthenticationError("Invalid or expired token")
        token.consumed_at = datetime.now(UTC)
        return user
