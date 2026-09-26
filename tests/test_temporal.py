from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

from backend_foundation.core.config import Settings
from backend_foundation.modules.temporal.client import connect_temporal
from backend_foundation.modules.temporal.module import temporal_health_checks, temporal_lifespan
from backend_foundation.modules.temporal.workflows import database_smoke_activity


@pytest.mark.asyncio
async def test_connect_temporal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    async def fake_connect(host: str, **kwargs: object) -> object:
        calls.update(host=host, **kwargs)
        return object()

    monkeypatch.setattr("backend_foundation.modules.temporal.client.Client.connect", fake_connect)
    settings = Settings(_env_file=None)
    await connect_temporal(settings, lazy=True)
    assert calls == {
        "host": "localhost:7233",
        "namespace": "backend-foundation",
        "lazy": True,
    }


@pytest.mark.asyncio
async def test_temporal_lifespan_and_health(monkeypatch: pytest.MonkeyPatch) -> None:
    class ServiceClient:
        async def check_health(self) -> None:
            return None

    class Temporal:
        service_client = ServiceClient()

    async def fake_connect(_settings: Settings, *, lazy: bool = False) -> Temporal:
        assert lazy
        return Temporal()

    monkeypatch.setattr("backend_foundation.modules.temporal.module.connect_temporal", fake_connect)
    app = FastAPI()
    async with temporal_lifespan(app, Settings(_env_file=None)):
        await temporal_health_checks(app)["temporal"]()
        assert app.state.temporal is not None
    assert app.state.temporal is None


@pytest.mark.asyncio
async def test_database_smoke_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        async def execute(self, _statement: object) -> None:
            return None

    @asynccontextmanager
    async def connect_context():
        yield Connection()

    class Engine:
        disposed = False

        def connect(self):
            return connect_context()

        async def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(
        "backend_foundation.modules.temporal.workflows.create_async_engine",
        lambda _url: engine,
    )
    assert await database_smoke_activity("postgresql://test") == "ok-workflow"
    assert engine.disposed
