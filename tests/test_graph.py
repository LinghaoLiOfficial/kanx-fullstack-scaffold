import pytest

from backend_foundation.modules.ai.graph import graph, run_smoke_graph, run_workflow


@pytest.mark.asyncio
async def test_smoke_graph() -> None:
    assert await run_smoke_graph("ok") == "ok-graph"
    assert (await graph.ainvoke({"value": "studio"}))["value"] == "studio-graph"


@pytest.mark.asyncio
async def test_default_workflow_runs_three_tasks_offline() -> None:
    result = await run_workflow("Can this ship?", "mock")
    assert result["summary"] == "Can this ship?-graph"
    assert result["extracted"]["word_count"] == 3
    assert result["classification"]["category"] == "question"
    assert [trace["task"] for trace in result["traces"]] == [
        "summarize",
        "extract",
        "classify",
    ]
    assert all(trace["status"] == "mock" for trace in result["traces"])
    assert all(trace["input"][0]["role"] == "system" for trace in result["traces"])
    assert all(trace["input"][1]["role"] == "user" for trace in result["traces"])
    assert all(trace["output_schema"] for trace in result["traces"])
    assert all("output_schema" in trace["input"][0]["content"] for trace in result["traces"])
    assert result["run"]["status"] == "success"
    assert result["run"]["total_llm_tasks"] == 3
    assert len(result["run"]["validation_results"]) == 4
    assert all(event["status"] == "success" for event in result["node_events"].values())
