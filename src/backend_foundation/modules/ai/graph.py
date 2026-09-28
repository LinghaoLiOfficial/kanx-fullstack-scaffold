from __future__ import annotations

import json
import operator
import time
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal, TypedDict
from urllib.parse import urlsplit

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from .provider import (
    StructuredInvocationError,
    content_hash,
    create_chat_model,
    invoke_structured_model,
    safe_error,
    utc_now,
)
from .settings import get_ai_settings

WorkflowMode = Literal["mock", "llm"]
WORKFLOW_STAGES = ("summarize", "extract", "classify")
WORKFLOW_EDGES = (("summarize", "extract"), ("extract", "classify"))

SUMMARIZE_SYSTEM_PROMPT = (
    "You are an expert in professional summarization. Produce a faithful, concise summary "
    "that preserves the source text's key intent and facts."
)
EXTRACT_SYSTEM_PROMPT = (
    "You are an expert in structured information extraction. Extract word count, character "
    "count, and up to three keywords supported by the source text. Conform to the schema."
)
CLASSIFY_SYSTEM_PROMPT = (
    "You are an expert in text intent classification. Classify the source text accurately "
    "as question, request, complaint, or general and provide a calibrated confidence score."
)


class SummaryOutput(BaseModel):
    summary: str = Field(min_length=1)


class ExtractedData(BaseModel):
    word_count: int = Field(ge=0)
    character_count: int = Field(ge=0)
    keywords: list[str] = Field(default_factory=list)


class Classification(BaseModel):
    category: str
    confidence: float = Field(ge=0, le=1)


class WorkflowState(TypedDict, total=False):
    value: str
    original_value: str
    mode: WorkflowMode
    summary: str
    extracted: dict[str, Any]
    classification: dict[str, Any]
    traces: Annotated[list[dict[str, Any]], operator.add]
    run: dict[str, Any]
    node_events: dict[str, dict[str, Any]]
    artifacts: dict[str, Any]
    validations: list[dict[str, Any]]
    error: str


def _new_run(value: str, mode: WorkflowMode) -> dict[str, Any]:
    return {
        "run_id": uuid.uuid4().hex,
        "workflow_name": "default-ai-workflow",
        "workflow_version": "1.0.0",
        "mode": mode,
        "started_at": utc_now(),
        "finished_at": None,
        "duration_ms": None,
        "status": "running",
        "input": value,
        "error": None,
        "total_llm_tasks": len(WORKFLOW_STAGES),
        "total_attempts": 0,
        "retry_count": 0,
        "total_tokens": 0,
        "validation_results": [],
    }


def _initial_node_events() -> dict[str, dict[str, Any]]:
    return {
        stage: {
            "node_name": stage,
            "status": "waiting",
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "error": None,
        }
        for stage in WORKFLOW_STAGES
    }


def _safe_error(error: Exception) -> str:
    # Provider exceptions can contain request URLs, headers or credentials.
    if isinstance(error, RuntimeError) and str(error).startswith("AI_API_KEY is required"):
        return "AI_API_KEY is required to create a chat model"
    return f"{type(error).__name__}: LLM call failed"


async def _call_task(
    state: WorkflowState,
    task: str,
    prompt: str,
    output_key: str,
    mock_output: object,
    output_schema: type[BaseModel],
) -> dict[str, Any]:
    value = state.get("original_value", state["value"])
    mode = state.get("mode", "mock")
    if mode not in ("mock", "llm"):
        raise ValueError("Workflow mode must be 'mock' or 'llm'")
    configured = get_ai_settings().for_task(task)
    json_schema = output_schema.model_json_schema()
    system_prompt = (
        f"{prompt}\nReturn only a valid JSON object matching this output_schema exactly:\n"
        f"{json.dumps(json_schema, ensure_ascii=False, separators=(',', ':'))}"
    )
    trace: dict[str, Any] = {
        "task": task,
        "mode": mode,
        "status": "mock" if mode == "mock" else "success",
        "model": configured.model if mode == "llm" else "deterministic mock",
        "provider": urlsplit(configured.base_url).hostname if mode == "llm" else None,
        "connect_timeout_seconds": configured.connect_timeout_seconds,
        "read_timeout_seconds": configured.read_timeout_seconds,
        "max_retries": configured.max_retries,
        "retry_policy": {
            "max_retries": configured.max_retries,
            "strategy": "exponential",
            "base_delay_seconds": 0.05,
        },
        "temperature": configured.temperature,
        "max_tokens": configured.max_tokens,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": value},
        ],
        "output_schema": json_schema,
        "actual_output": None,
        "parsed_output": None,
        "usage": {},
        "response_metadata": {},
        "error": None,
        "semantic_role": task,
        "input_hash": content_hash(value),
        "prompt_hash": content_hash(system_prompt),
        "schema_hash": content_hash(json_schema),
        "attempts": [],
        "call_started_at": utc_now(),
    }
    started = time.perf_counter()
    try:
        output = mock_output
        if mode == "mock":
            trace["attempts"] = [
                {
                    "attempt_number": 1,
                    "started_at": trace["call_started_at"],
                    "finished_at": None,
                    "duration_ms": None,
                    "status": "success",
                    "raw_output": output,
                    "parsed_output": output,
                    "provider": "deterministic mock",
                    "model": "deterministic mock",
                    "error": None,
                    "retry_reason": None,
                }
            ]
        if mode == "llm":
            model = create_chat_model(task=task)
            messages = [SystemMessage(content=system_prompt), HumanMessage(content=value)]
            response = await invoke_structured_model(
                model,
                output_schema,
                messages,
                max_retries=configured.max_retries,
            )
            raw = response["raw"]
            trace["attempts"] = response["attempts"]
            for attempt in trace["attempts"]:
                attempt.update(provider=trace["provider"], model=trace["model"])
            trace["actual_output"] = raw.model_dump(
                mode="json", include={"content", "tool_calls", "invalid_tool_calls"}
            )
            output = response["parsed"]
            trace["usage"] = raw.usage_metadata or {}
            # Only allow known non-sensitive fields, never arbitrary provider headers.
            trace["response_metadata"] = {
                key: raw.response_metadata[key]
                for key in ("model_name", "finish_reason", "system_fingerprint", "token_usage")
                if key in raw.response_metadata
            }
        trace["parsed_output"] = output
        return {output_key: output, "traces": [trace]}
    except StructuredInvocationError as error:
        trace["attempts"] = error.attempts
        trace["status"] = "error"
        trace["error"] = safe_error(error)
        return {"error": f"{task}: {trace['error']}", "traces": [trace]}
    except Exception as error:
        trace["status"] = "error"
        trace["error"] = _safe_error(error)
        return {"error": f"{task}: {trace['error']}", "traces": [trace]}
    finally:
        trace["duration_ms"] = round((time.perf_counter() - started) * 1000)
        trace["finished_at"] = utc_now()
        for attempt in trace.get("attempts", []):
            attempt["finished_at"] = attempt.get("finished_at") or trace["finished_at"]
            attempt["duration_ms"] = attempt.get("duration_ms") or trace["duration_ms"]


async def _summarize(state: WorkflowState) -> dict[str, Any]:
    value = state["value"]
    result = await _call_task(
        state,
        "summarize",
        SUMMARIZE_SYSTEM_PROMPT,
        "summary",
        {"summary": f"{value}-graph"},
        SummaryOutput,
    )
    if "summary" in result:
        if state.get("mode", "mock") == "llm":
            result["summary"] = result["summary"]["summary"]
        else:
            result["summary"] = f"{value}-graph"
    # Keep the original smoke contract without changing downstream task inputs.
    if state.get("mode", "mock") == "mock":
        result.update(value=f"{value}-graph", original_value=value)
    return result


async def _extract(state: WorkflowState) -> dict[str, Any]:
    value = state.get("original_value", state["value"])
    words = value.split()
    return await _call_task(
        state,
        "extract",
        EXTRACT_SYSTEM_PROMPT,
        "extracted",
        ExtractedData(
            word_count=len(words),
            character_count=len(value),
            keywords=list(dict.fromkeys(words[:3])),
        ).model_dump(),
        ExtractedData,
    )


async def _classify(state: WorkflowState) -> dict[str, Any]:
    value = state.get("original_value", state["value"])
    return await _call_task(
        state,
        "classify",
        CLASSIFY_SYSTEM_PROMPT,
        "classification",
        Classification(
            category="question" if value.rstrip().endswith(("?", "？")) else "general",
            confidence=1.0,
        ).model_dump(),
        Classification,
    )


def _continue_or_end(state: WorkflowState) -> str:
    return "end" if state.get("error") else "continue"


def _validate_state(state: WorkflowState) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for trace in state.get("traces", []):
        results.append(
            {
                "name": f"{trace['task']}.output_schema_valid",
                "category": "schema",
                "status": "passed"
                if trace.get("parsed_output") is not None
                else "failed",
                "message": "Output matches the declared schema"
                if trace.get("parsed_output") is not None
                else "No valid parsed output",
                "duration_ms": 0,
            }
        )
    results.append(
        {
            "name": "workflow_completed",
            "category": "workflow",
            "status": "passed"
            if not state.get("error")
            and len(state.get("traces", [])) == len(WORKFLOW_STAGES)
            else "failed",
            "message": "All workflow stages completed"
            if not state.get("error")
            else "Workflow stopped with an error",
            "duration_ms": 0,
        }
    )
    return results


def _refresh_run(state: WorkflowState, *, finished: bool = False) -> None:
    run = state.setdefault("run", _new_run(state.get("value", ""), state.get("mode", "mock")))
    traces = state.get("traces", [])
    attempts = [attempt for trace in traces for attempt in trace.get("attempts", [])]
    run["total_attempts"] = len(attempts)
    run["retry_count"] = max(0, len(attempts) - len(traces))
    run["total_tokens"] = sum(
        int(trace.get("usage", {}).get("total_tokens", 0) or 0) for trace in traces
    )
    if finished:
        run["finished_at"] = utc_now()
        run["duration_ms"] = round(
            (datetime.now(UTC) - datetime.fromisoformat(run["started_at"])).total_seconds() * 1000
        )
        run["status"] = "failed" if state.get("error") else "success"
        run["error"] = state.get("error")
        state["validations"] = _validate_state(state)
        run["validation_results"] = state["validations"]


async def stream_workflow_events(
    value: str, mode: WorkflowMode = "mock"
) -> AsyncIterator[WorkflowState]:
    if not value.strip():
        raise ValueError("Workflow input must not be empty")
    if mode not in ("mock", "llm"):
        raise ValueError("Workflow mode must be 'mock' or 'llm'")
    state: WorkflowState = {
        "value": value,
        "mode": mode,
        "run": _new_run(value, mode),
        "node_events": _initial_node_events(),
        "artifacts": {},
        "validations": [],
        "traces": [],
    }
    yield deepcopy(state)
    try:
        async for event in compiled_graph.astream_events(
            {"value": value, "mode": mode}, version="v2"
        ):
            name = event.get("name")
            if isinstance(name, str) and name.startswith("_"):
                name = name[1:]
            if name not in WORKFLOW_STAGES:
                continue
            event_type = event.get("event")
            node = state["node_events"][name]
            if event_type == "on_chain_start":
                node.update(status="running", started_at=utc_now(), error=None)
                yield deepcopy(state)
            elif event_type == "on_chain_end":
                output = event.get("data", {}).get("output") or {}
                state["traces"] = [*state.get("traces", []), *output.get("traces", [])]
                for key, item in output.items():
                    if key != "traces":
                        state[key] = item
                if output.get("error"):
                    node.update(status="failed", finished_at=utc_now(), error=output["error"])
                    state["error"] = output["error"]
                    failed_index = WORKFLOW_STAGES.index(name)
                    for skipped in WORKFLOW_STAGES[failed_index + 1 :]:
                        state["node_events"][skipped]["status"] = "skipped"
                    _refresh_run(state, finished=True)
                    yield deepcopy(state)
                    return
                node.update(status="success", finished_at=utc_now())
                if node.get("started_at"):
                    node["duration_ms"] = round(
                        (
                            datetime.fromisoformat(node["finished_at"])
                            - datetime.fromisoformat(node["started_at"])
                        ).total_seconds()
                        * 1000
                    )
                _refresh_run(state)
                yield deepcopy(state)
            elif event_type == "on_chain_error":
                node.update(status="failed", finished_at=utc_now(), error="Node execution failed")
                state["error"] = f"{name}: node execution failed"
                _refresh_run(state, finished=True)
                yield deepcopy(state)
                return
        _refresh_run(state, finished=True)
        yield deepcopy(state)
    except Exception as error:
        state["error"] = safe_error(error)
        _refresh_run(state, finished=True)
        yield deepcopy(state)



def build_workflow() -> Any:
    workflow = StateGraph(WorkflowState)
    nodes = {"summarize": _summarize, "extract": _extract, "classify": _classify}
    for stage in WORKFLOW_STAGES:
        workflow.add_node(stage, nodes[stage])
    workflow.add_edge(START, WORKFLOW_STAGES[0])
    for source, target in WORKFLOW_EDGES:
        workflow.add_conditional_edges(
            source, _continue_or_end, {"continue": target, "end": END}
        )
    workflow.add_edge(WORKFLOW_STAGES[-1], END)
    return workflow.compile()


compiled_graph: Any = build_workflow()
graph = compiled_graph


async def stream_workflow(
    value: str, mode: WorkflowMode = "mock"
) -> AsyncIterator[WorkflowState]:
    async for state in stream_workflow_events(value, mode):
        yield state


async def run_workflow(value: str, mode: WorkflowMode = "mock") -> WorkflowState:
    result: WorkflowState = {}
    async for state in stream_workflow(value, mode):
        result = state
    return result


async def run_smoke_graph(value: str) -> str:
    result = await run_workflow(value, "mock")
    return str(result["summary"])
