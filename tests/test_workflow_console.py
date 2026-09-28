import pytest
from langchain_core.messages import AIMessage

from backend_foundation.modules.ai import graph as graph_module
from backend_foundation.modules.ai.gradio_console import EXAMPLES, _workflow_topology, run_console
from backend_foundation.modules.ai.settings import AISettings


@pytest.mark.asyncio
async def test_console_streams_each_task_and_resets_outputs() -> None:
    frames = [frame async for frame in run_console(EXAMPLES["Question"], "mock")]
    assert frames[0][1][0][1] == "waiting"
    assert any(frame[1][0][1] == "running" for frame in frames)
    assert frames[0][2] is None
    assert frames[-1][0] == "Completed (mock, no LLM calls)"
    assert all(row[1] == "success" for row in frames[-1][1])
    assert len(frames[-1]) == 26
    assert frames[-1][-9]["workflow_name"] == "default-ai-workflow"
    assert len(frames[-1][-8]) == 3
    assert frames[-1][-7][-1]["name"] == "workflow_completed"
    assert frames[-1][-6] == {}
    assert "tasks" in frames[-1][-5]
    assert "tasks" in frames[-1][-4]
    assert "calls" in frames[-1][-3]
    assert len(frames[-1][-2]["traces"]) == 3
    assert frames[-1][-1].count("<rect") == 3
    assert frames[-1][-1].count("marker-end") == 2


@pytest.mark.asyncio
async def test_llm_trace_keeps_raw_and_parsed_responses(monkeypatch) -> None:
    tasks = []
    messages = []

    class FakeModel:
        schema = None

        def with_structured_output(self, schema, *, method, include_raw):
            assert method == "json_mode"
            assert include_raw is True
            self.schema = schema
            return self

        async def ainvoke(self, input_messages):
            messages.append(input_messages)
            raw = AIMessage(
                content="Test summary",
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                response_metadata={"model_name": "fake-model", "finish_reason": "stop"},
            )
            if self.schema is graph_module.SummaryOutput:
                parsed = self.schema(summary="Test summary")
            elif self.schema is graph_module.ExtractedData:
                parsed = self.schema(word_count=2, character_count=11, keywords=["Test"])
            elif self.schema is graph_module.Classification:
                parsed = self.schema(category="general", confidence=0.9)
            return {"raw": raw, "parsed": parsed, "parsing_error": None}

    def create_model(*, task):
        tasks.append(task)
        return FakeModel()

    monkeypatch.setattr(graph_module, "create_chat_model", create_model)
    monkeypatch.setattr(
        graph_module,
        "get_ai_settings",
        lambda: AISettings(_env_file=None, api_key="private-test-key"),
    )
    result = await graph_module.run_workflow("Test input.", "llm")
    assert tasks == ["summarize", "extract", "classify"]
    assert all(item[0].type == "system" and item[1].type == "human" for item in messages)
    assert all("output_schema" in str(item[0].content) for item in messages)
    assert all(trace["actual_output"]["content"] == "Test summary" for trace in result["traces"])
    assert all(trace["usage"]["total_tokens"] == 15 for trace in result["traces"])
    assert result["traces"][1]["parsed_output"]["word_count"] == 2
    assert result["summary"] == "Test summary"
    assert "private-test-key" not in str(result)


@pytest.mark.asyncio
async def test_failed_call_is_visible_and_skips_remaining_nodes(monkeypatch) -> None:
    def failing_model(*, task):
        raise RuntimeError("secret-provider-header")

    monkeypatch.setattr(graph_module, "create_chat_model", failing_model)
    frames = [frame async for frame in run_console("Test input.", "llm")]
    assert [row[1] for row in frames[-1][1]] == ["failed", "skipped", "skipped"]
    assert len(frames[-1][-2]["traces"]) == 1
    assert "secret-provider-header" not in str(frames)


@pytest.mark.asyncio
async def test_empty_input_does_not_start_calls() -> None:
    frames = [frame async for frame in run_console("  ", "llm")]
    assert len(frames) == 1
    assert frames[0][0] == "Please enter text"
    assert "traces" not in frames[0][-2]


def test_topology_uses_workflow_stages_edges_status_and_duration() -> None:
    html = _workflow_topology(
        {
            "value": "test",
            "mode": "mock",
            "traces": [
                {"task": "summarize", "status": "mock", "duration_ms": 12},
            ],
        },
        running_elapsed_ms=250,
    )
    assert html.count("<rect") == 3
    assert html.count("marker-end") == 2
    assert 'viewBox="0 0 360 430"' in html
    assert 'x1="180" y1="104" x2="180" y2="166"' in html
    assert "success · 12 ms" in html
    assert "running · 250 ms" in html
