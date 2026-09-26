import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from backend_foundation.core.application import create_app
from backend_foundation.core.config import Settings
from backend_foundation.core.health import readiness
from backend_foundation.core.modules import ModuleSpec


@pytest.mark.asyncio
async def test_liveness_and_request_id() -> None:
    app = create_app(Settings(_env_file=None), modules=())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live", headers={"X-Request-ID": "known-id"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"] == "known-id"


@pytest.mark.asyncio
async def test_credentialed_cors_contract() -> None:
    app = create_app(
        Settings(_env_file=None, cors_allowed_origins="http://localhost:3000"), modules=()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        preflight = await client.options(
            "/health/live",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Request-ID",
            },
        )
        response = await client.get("/health/live", headers={"Origin": "http://localhost:3000"})
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert preflight.headers["access-control-allow-credentials"] == "true"
    assert response.headers["access-control-expose-headers"] == "X-Request-ID"


@pytest.mark.asyncio
async def test_readiness_success_failure_and_timeout() -> None:
    async def ok() -> None:
        return None

    async def fail() -> None:
        raise RuntimeError("unavailable")

    async def slow() -> None:
        await asyncio.sleep(1)

    body, status = await readiness({"database": ok}, 0.1)
    assert status == 200
    assert body["checks"] == {"database": "ok"}

    body, status = await readiness({"failure": fail, "timeout": slow}, 0.01)
    assert status == 503
    assert body["checks"] == {"failure": "error", "timeout": "error"}


@pytest.mark.asyncio
async def test_module_lifespan_health_and_router() -> None:
    events: list[str] = []
    router = APIRouter()

    @router.get("/module")
    async def route() -> dict[str, str]:
        return {"module": "ok"}

    @asynccontextmanager
    async def lifespan(app: Any, _settings: Settings):
        events.append("start")
        app.state.resource = True
        try:
            yield
        finally:
            events.append("stop")

    async def check() -> None:
        return None

    module = ModuleSpec(
        name="test",
        routers=(router,),
        lifespan=lifespan,
        health_checks=lambda _app: {"test": check},
    )
    app = create_app(Settings(_env_file=None), modules=(module,))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/module")).status_code == 200
            response = await client.get("/health/ready")
            assert response.json()["checks"] == {"test": "ok"}
    assert events == ["start", "stop"]


@pytest.mark.asyncio
async def test_error_envelope_and_request_id_generation() -> None:
    app = create_app(Settings(_env_file=None), modules=())

    @app.get("/number/{value}")
    async def number(value: int) -> int:
        return value

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        missing = await client.get("/missing")
        invalid = await client.get("/number/nope")
    assert missing.json()["error"]["code"] == "http_404"
    assert invalid.json()["error"]["code"] == "validation_error"
    assert missing.headers["X-Request-ID"] == missing.json()["error"]["request_id"]
