import importlib
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

from backend_foundation.core.config import Settings
from backend_foundation.modules.database.base import Base
from backend_foundation.modules.database.module import database_health_checks, database_lifespan
from backend_foundation.modules.database.session import (
    create_engine,
    create_session_factory,
    session_dependency,
)


def test_create_engine_factory_and_metadata() -> None:
    engine = create_engine(Settings(_env_file=None))
    factory = create_session_factory(engine)
    assert factory.kw["expire_on_commit"] is False
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"


@pytest.mark.asyncio
async def test_session_dependency_rolls_back_on_error() -> None:
    class Session:
        rolled_back = False

        async def rollback(self) -> None:
            self.rolled_back = True

    session = Session()

    @asynccontextmanager
    async def context():
        yield session

    def factory():
        return context()

    dependency = session_dependency(factory)  # type: ignore[arg-type]
    assert await anext(dependency) is session
    with pytest.raises(RuntimeError):
        await dependency.athrow(RuntimeError("failed"))
    assert session.rolled_back


@pytest.mark.asyncio
async def test_database_module_lifecycle_and_health(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        async def execute(self, _statement: object) -> None:
            return None

    @asynccontextmanager
    async def connection_context():
        yield Connection()

    class Engine:
        disposed = False

        def connect(self):
            return connection_context()

        async def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    database_module = importlib.import_module("backend_foundation.modules.database.module")
    monkeypatch.setattr(database_module, "create_engine", lambda _settings: engine)
    monkeypatch.setattr(
        database_module,
        "create_session_factory",
        lambda _engine: object(),
    )
    app = FastAPI()
    async with database_lifespan(app, Settings(_env_file=None)):
        check = database_health_checks(app)["database"]
        await check()
    assert engine.disposed
