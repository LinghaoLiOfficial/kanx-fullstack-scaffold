from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@activity.defn
async def database_smoke_activity(database_url: str) -> str:
    engine: AsyncEngine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return "ok-workflow"
    finally:
        await engine.dispose()


@workflow.defn
class SmokeWorkflow:
    @workflow.run
    async def run(self, database_url: str) -> str:
        return await workflow.execute_activity(
            database_smoke_activity,
            database_url,
            start_to_close_timeout=timedelta(seconds=30),
        )
