from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from backend_foundation.core.config import get_settings
from backend_foundation.core.modules import JobHandlerSpec, ModuleSpec, load_profile_modules
from backend_foundation.modules.jobs.service import PermanentJobError, RetryableJobError
from backend_foundation.modules.temporal.worker import run_worker

attempts: dict[str, int] = {}


async def flaky(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    attempts[job_id] = attempts.get(job_id, 0) + 1
    if attempts[job_id] < int(payload.get("succeed_on", 3)):
        raise RuntimeError("integration retry")
    return {"handler_attempts": attempts[job_id]}


async def slow(_job_id: str, _payload: dict[str, Any]) -> None:
    await asyncio.sleep(300)


async def permanent(_job_id: str, _payload: dict[str, Any]) -> None:
    raise PermanentJobError("integration permanent failure", code="integration_permanent")


async def exponential(_job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(payload["attempt_log"]))
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    timestamps = [*filter(None, previous.splitlines()), str(time.time())]
    path.write_text("\n".join(timestamps) + "\n", encoding="utf-8")
    if len(timestamps) < int(payload.get("succeed_on", 3)):
        raise RetryableJobError("integration transient failure", code="integration_transient")
    return {"handler_attempts": len(timestamps)}


async def main() -> None:
    settings = get_settings()
    modules = (
        *load_profile_modules(settings),
        ModuleSpec(
            name="integration-handlers",
            job_handlers=(
                JobHandlerSpec("integration.flaky", flaky),
                JobHandlerSpec("integration.slow", slow),
                JobHandlerSpec("integration.permanent", permanent),
                JobHandlerSpec("integration.exponential", exponential),
            ),
        ),
    )
    await run_worker(settings, modules)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
