from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response

from backend_foundation.modules.auth.dependencies import AuthPrincipal
from backend_foundation.modules.auth.router import (
    ChangePasswordRequest,
    EmailRequest,
    LoginRequest,
    RegisterRequest,
    ResetRequest,
    TokenRequest,
    change_password,
    forgot_password,
    login,
    logout,
    me,
    refresh,
    register,
    resend_verification,
    reset_password,
    verify_email,
)
from backend_foundation.modules.auth.service import AuthenticationError, TokenPair, password_hash


class Session:
    def __init__(self, scalar: object | None = None) -> None:
        self.value = scalar
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def scalar(self, _query: object) -> object | None:
        return self.value


def request(origin: str = "http://localhost:3000") -> Mock:
    value = Mock()
    value.headers = {"origin": origin}
    return value


@pytest.mark.asyncio
async def test_register_verify_login_and_reset_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Mock()
    service.register = AsyncMock()
    service.verify_email = AsyncMock()
    service.login = AsyncMock(return_value=TokenPair("access", "refresh", "csrf", 900))
    service.reset_password = AsyncMock()
    service.forgot_password = AsyncMock()
    monkeypatch.setattr("backend_foundation.modules.auth.router.AuthService", lambda: service)
    session = Session()
    body = RegisterRequest(
        email="person@example.com", password="secure-password-123", display_name="Person"
    )
    assert "verification email" in (await register(body, session))["message"]  # type: ignore[arg-type]
    assert (await verify_email(TokenRequest(token="token"), session))["message"] == (  # type: ignore[arg-type]
        "Email verified"
    )
    response = Response()
    tokens = await login(  # type: ignore[arg-type]
        LoginRequest(email="person@example.com", password="secure-password-123"),
        response,
        session,
    )
    assert tokens["access_token"] == "access"
    assert len(response.raw_headers) >= 2
    assert any(
        b"csrf_token=" in value and b"Path=/" in value
        for key, value in response.raw_headers
        if key == b"set-cookie"
    )
    assert (
        "reset email"
        in (
            await forgot_password(EmailRequest(email="person@example.com"), session)  # type: ignore[arg-type]
        )["message"]
    )
    assert (
        await reset_password(
            ResetRequest(token="token", password="another-secure-password"),
            session,  # type: ignore[arg-type]
        )
    )["message"] == "Password reset"

    service.verify_email.side_effect = AuthenticationError("bad token")
    with pytest.raises(HTTPException) as error:
        await verify_email(TokenRequest(token="bad"), session)  # type: ignore[arg-type]
    assert error.value.status_code == 400
    service.login.side_effect = AuthenticationError("bad login")
    with pytest.raises(HTTPException) as error:
        await login(  # type: ignore[arg-type]
            LoginRequest(email="person@example.com", password="bad"), Response(), session
        )
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_refresh_logout_and_resend_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Mock()
    service.rotate = AsyncMock(return_value=TokenPair("access", "new-refresh", "new-csrf", 900))
    service.revoke_family = AsyncMock()
    service.users = Mock()
    service.users.by_email = AsyncMock(return_value=None)
    monkeypatch.setattr("backend_foundation.modules.auth.router.AuthService", lambda: service)
    session = Session()
    response = Response()
    result = await refresh(  # type: ignore[arg-type]
        request(), response, session, "refresh", "csrf", "csrf"
    )
    assert result["access_token"] == "access"
    with pytest.raises(HTTPException) as error:
        await refresh(request(), Response(), session, "refresh", "csrf", "different")  # type: ignore[arg-type]
    assert error.value.status_code == 403
    assert (
        "verification email"
        in (
            await resend_verification(EmailRequest(email="missing@example.com"), session)  # type: ignore[arg-type]
        )["message"]
    )
    await logout(request(), Response(), session, None, "csrf", "csrf")  # type: ignore[arg-type]
    with pytest.raises(HTTPException):
        await logout(request(), Response(), session, None, "csrf", "wrong")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_change_password_and_me(monkeypatch: pytest.MonkeyPatch) -> None:
    account = SimpleNamespace(
        id="user",
        email="person@example.test",
        display_name="Person",
        password_hash=password_hash.hash("current-password-123"),
        token_version=1,
    )
    service = Mock()
    service.users = Mock()
    service.users.by_id = AsyncMock(return_value=account)
    service.revoke_user_sessions = AsyncMock()
    service.email = Mock()
    service.email.enqueue = AsyncMock()
    monkeypatch.setattr("backend_foundation.modules.auth.router.AuthService", lambda: service)
    principal = AuthPrincipal("user", account.email, account.display_name, datetime.now(UTC))
    result = await change_password(  # type: ignore[arg-type]
        ChangePasswordRequest(
            current_password="current-password-123", new_password="changed-password-123"
        ),
        principal,
        Session(),
    )
    assert result == {"message": "Password changed"}
    assert service.revoke_user_sessions.await_count == 1
    assert (await me(principal))["email_verified"] is True
