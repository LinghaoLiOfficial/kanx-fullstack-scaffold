import pytest

from backend_foundation.modules.ai.graph import graph, run_smoke_graph


@pytest.mark.asyncio
async def test_smoke_graph() -> None:
    assert await run_smoke_graph("ok") == "ok-graph"
    assert (await graph.ainvoke({"value": "studio"}))["value"] == "studio-graph"
