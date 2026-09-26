from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend_foundation.modules.auth.dependencies import (
    AuthPrincipal,
    current_user,
    require_permission,
    require_verified_user,
)
from backend_foundation.modules.auth.models import AuthSession, AuthToken, AuthTokenType
from backend_foundation.modules.auth.router import _validate_origin
from backend_foundation.modules.auth.service import (
    AuthenticationError,
    AuthService,
    RefreshTokenReuseError,
    digest,
    password_hash,
)
from backend_foundation.modules.auth.settings import AuthSettings
from backend_foundation.modules.users.service import DuplicateUserError, UserService


def test_refresh_origin_is_required_and_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = AuthSettings(_env_file=None, allowed_origins="https://app.example")
    monkeypatch.setattr(
        "backend_foundation.modules.auth.router.get_auth_settings", lambda: settings
    )
    missing = Mock(headers={})
    with pytest.raises(HTTPException) as missing_error:
        _validate_origin(missing)
    assert missing_error.value.status_code == 403
    denied = Mock(headers={"origin": "https://evil.example"})
    with pytest.raises(HTTPException) as denied_error:
        _validate_origin(denied)
    assert denied_error.value.status_code == 403
    _validate_origin(Mock(headers={"origin": "https://app.example"}))


def test_auth_secret_is_not_exposed() -> None:
    settings = AuthSettings(_env_file=None, jwt_secret="a-unique-secret-longer-than-32-characters")
    assert "a-unique-secret" not in repr(settings)


class FakeSession:
    def __init__(self, scalar_values: list[object | None] | None = None) -> None:
        self.scalar_values = list(scalar_values or [])
        self.added: list[object] = []
        self.executed: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if isinstance(value, AuthSession) and value.id is None:
                value.id = "session-id"

    async def scalar(self, _query: object) -> object | None:
        return self.scalar_values.pop(0) if self.scalar_values else None

    async def execute(self, query: object) -> None:
        self.executed.append(query)


def account(**overrides: object) -> SimpleNamespace:
    values = {
        "id": "user-id",
        "email": "person@example.test",
        "normalized_email": "person@example.test",
        "password_hash": password_hash.hash("secure-password-123"),
        "display_name": "Person",
        "status": "active",
        "email_verified_at": datetime.now(UTC),
        "token_version": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def service_with_fakes(user: object | None = None) -> tuple[AuthService, Mock, Mock, Mock]:
    users = Mock()
    users.create = AsyncMock(return_value=user or account())
    users.by_email = AsyncMock(return_value=user)
    users.by_id = AsyncMock(return_value=user)
    users.active.side_effect = lambda value: value.status == "active"
    users.activate.side_effect = UserService.activate
    users.change_password.side_effect = UserService.change_password
    users.disable.side_effect = UserService.disable
    email = Mock()
    email.enqueue = AsyncMock()
    organizations = Mock()
    organizations.create_personal = AsyncMock(return_value="org-id")
    settings = AuthSettings(
        _env_file=None,
        jwt_secret="a-unique-secret-longer-than-32-characters",
        jwt_issuer="issuer",
        jwt_audience="audience",
    )
    return AuthService(settings, users, email, organizations), users, email, organizations


@pytest.mark.asyncio
async def test_register_login_and_enumeration_safety() -> None:
    user = account(email_verified_at=None, status="pending")
    service, users, email, organizations = service_with_fakes(user)
    session = FakeSession()
    assert (
        await service.register(  # type: ignore[arg-type]
            session, email=user.email, password="secure-password-123", display_name="Person"
        )
        is user
    )
    organizations.create_personal.assert_awaited_once()
    assert any(isinstance(item, AuthToken) for item in session.added)
    email.enqueue.assert_awaited_once()
    with pytest.raises(AuthenticationError, match="12 characters"):
        await service.register(  # type: ignore[arg-type]
            session, email=user.email, password="short", display_name="Person"
        )
    users.create.side_effect = DuplicateUserError("duplicate")
    with pytest.raises(AuthenticationError, match="Unable to register"):
        await service.register(  # type: ignore[arg-type]
            session, email=user.email, password="secure-password-123", display_name="Person"
        )
    with pytest.raises(AuthenticationError, match="verification"):
        await service.login(session, user.email, "secure-password-123")  # type: ignore[arg-type]
    user.status = "active"
    user.email_verified_at = datetime.now(UTC)
    tokens = await service.login(session, user.email, "secure-password-123")  # type: ignore[arg-type]
    assert tokens.access_token and tokens.refresh_token and tokens.csrf_token


@pytest.mark.asyncio
async def test_one_time_tokens_password_reset_and_disable() -> None:
    user = account()
    service, users, email, _organizations = service_with_fakes(user)
    token = AuthToken(
        user_id=user.id,
        token_type=AuthTokenType.VERIFY_EMAIL,
        digest=digest("verify"),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = FakeSession([token])
    assert await service.verify_email(session, "verify") is user  # type: ignore[arg-type]
    assert token.consumed_at is not None
    assert user.status == "active"

    users.by_email.return_value = None
    await service.forgot_password(session, "missing@example.test")  # type: ignore[arg-type]
    users.by_email.return_value = user
    await service.forgot_password(session, user.email)  # type: ignore[arg-type]
    email.enqueue.assert_awaited()

    reset = AuthToken(
        user_id=user.id,
        token_type=AuthTokenType.RESET_PASSWORD,
        digest=digest("reset"),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    reset_session = FakeSession([reset])
    old_version = user.token_version
    await service.reset_password(  # type: ignore[arg-type]
        reset_session, "reset", "another-secure-password"
    )
    assert user.token_version == old_version + 1
    assert reset_session.executed
    await service.disable_user(reset_session, user)  # type: ignore[arg-type]
    assert user.status == "disabled"
    assert len(reset_session.executed) == 2


@pytest.mark.asyncio
async def test_refresh_rotation_and_reuse_detection() -> None:
    user = account()
    service, _users, _email, _organizations = service_with_fakes(user)
    current = AuthSession(
        id="old-session",
        user_id=user.id,
        family_id="family",
        refresh_digest=digest("refresh"),
        csrf_digest=digest("csrf"),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session = FakeSession([current])
    rotated = await service.rotate(session, "refresh", "csrf")  # type: ignore[arg-type]
    assert rotated.refresh_token != "refresh"
    assert current.rotated_at is not None
    reused = AuthSession(
        user_id=user.id,
        family_id="family",
        refresh_digest=digest("old"),
        csrf_digest=digest("csrf"),
        expires_at=datetime.now(UTC) + timedelta(days=1),
        rotated_at=datetime.now(UTC),
    )
    reuse_session = FakeSession([reused])
    with pytest.raises(RefreshTokenReuseError):
        await service.rotate(reuse_session, "old", "csrf")  # type: ignore[arg-type]
    assert reuse_session.executed


@pytest.mark.asyncio
async def test_access_token_dependency_and_permission_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AuthSettings(
        _env_file=None,
        jwt_secret="a-unique-secret-longer-than-32-characters",
        jwt_issuer="issuer",
        jwt_audience="audience",
    )
    monkeypatch.setattr(
        "backend_foundation.modules.auth.dependencies.get_auth_settings", lambda: settings
    )
    user = account()
    auth_session = AuthSession(
        id="session-id",
        user_id=user.id,
        family_id="family",
        refresh_digest=digest("refresh"),
        csrf_digest=digest("csrf"),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session = Mock()
    session.get = AsyncMock(return_value=auth_session)
    monkeypatch.setattr(
        "backend_foundation.modules.auth.dependencies.UserService.by_id",
        AsyncMock(return_value=user),
    )
    token = jwt.encode(
        {
            "sub": user.id,
            "sid": auth_session.id,
            "ver": user.token_version,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    principal = await current_user(  # type: ignore[arg-type]
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), session
    )
    assert principal.email_verified
    assert await require_verified_user(principal) is principal
    with pytest.raises(HTTPException, match="Verified email"):
        await require_verified_user(AuthPrincipal("id", "e", "n", None))

    role_require = AsyncMock()
    monkeypatch.setattr(
        "backend_foundation.modules.auth.dependencies.RoleService.require", role_require
    )
    dependency = require_permission("files:read")
    assert await dependency("org", principal, session) is principal
    role_require.assert_awaited_once_with(session, principal.id, "org", "files:read")

    with pytest.raises(HTTPException, match="Authentication required"):
        await current_user(None, session)  # type: ignore[arg-type]
