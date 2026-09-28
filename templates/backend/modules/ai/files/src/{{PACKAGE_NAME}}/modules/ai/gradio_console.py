"""Shared local Gradio console for the example LangGraph workflow."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from html import escape
from typing import Any

import gradio as gr

from .graph import (
    WORKFLOW_EDGES,
    WORKFLOW_STAGES,
    WorkflowMode,
    WorkflowState,
    stream_workflow_events,
)
from .settings import get_ai_settings

TASKS = WORKFLOW_STAGES
WORKFLOW_CSS = """
.workflow-examples button { min-height: 36px; }
.workflow-status textarea { font-weight: 600; }
.workflow-task {
    border: 1px solid var(--border-color-primary);
    border-radius: 8px;
    padding: 12px;
    background: var(--background-fill-secondary);
}
.workflow-task h3 { margin-top: 0; }
.workflow-topology {
    overflow-x: auto;
    border: 1px solid var(--border-color-primary);
    border-radius: 8px;
    padding: 12px;
    background: var(--background-fill-secondary);
}
.workflow-topology svg { display: block; min-width: 320px; width: 100%; height: 430px; }
.workflow-topology .edge { stroke: var(--body-text-color); stroke-width: 2; opacity: .7; }
.workflow-topology .node { stroke-width: 2; }
.workflow-topology .node-label { fill: var(--body-text-color); font-size: 14px; font-weight: 650; text-anchor: middle; }
.workflow-topology .node-meta { fill: var(--body-text-color-subdued); font-size: 11px; text-anchor: middle; }
"""
EXAMPLES = {
    "News": (
        "A city library opened a new reading room on Monday. It has 120 seats and "
        "will stay open until 9 p.m. The project was funded by local donations."
    ),
    "Question": (
        "What is the difference between a LangGraph workflow and a single LLM call?"
    ),
    "Request": (
        "Please prepare a concise launch checklist for our backend service. Include "
        "database migrations, API health checks, and rollback preparation."
    ),
    "Complaint": (
        "I paid for next-day delivery, but my order arrived four days late and the "
        "package was damaged. Please arrange a refund and explain what happened."
    ),
    "Technical report": (
        "Incident report: API latency rose from 180 ms to 2.4 seconds at 14:05 UTC. "
        "Database connection saturation was identified as the cause. We reduced "
        "worker concurrency from 100 to 30 at 14:20, restoring normal latency. "
        "No customer data was lost. Follow-up: add connection-pool alerts."
    ),
    "Mixed intent": (
        "Your new dashboard looks excellent, but the export button is broken. "
        "Could you investigate this before Friday and send me the CSV by email?"
    ),
}

STATUS_COLORS = {
    "waiting": "#94a3b8",
    "running": "#f59e0b",
    "success": "#22c55e",
    "failed": "#ef4444",
    "skipped": "#64748b",
}


def _workflow_topology(
    state: WorkflowState,
    stopped: bool = False,
    running_elapsed_ms: int | None = None,
    active: bool = True,
) -> str:
    traces = {trace["task"]: trace for trace in state.get("traces", [])}
    failed = bool(state.get("error")) or stopped
    statuses: dict[str, str] = {}
    active_assigned = False
    for task in TASKS:
        trace = traces.get(task)
        node_event = state.get("node_events", {}).get(task, {})
        if task in state.get("node_events", {}):
            statuses[task] = str(node_event["status"])
        elif trace:
            trace_status = str(trace.get("status", "waiting"))
            statuses[task] = {"mock": "success", "error": "failed"}.get(
                trace_status, trace_status
            )
        elif failed:
            statuses[task] = "skipped"
        elif active and not active_assigned:
            statuses[task] = "running"
            active_assigned = True
        else:
            statuses[task] = "waiting"
    width = 360
    height = max(430, len(TASKS) * 130 + 40)
    center_x = width // 2
    centers = [70 + index * 130 for index in range(len(TASKS))]
    positions = dict(zip(TASKS, centers, strict=True))
    edges = "".join(
        f'<line class="edge" x1="{center_x}" y1="{positions[source] + 34}" '
        f'x2="{center_x}" y2="{positions[target] - 34}" marker-end="url(#workflow-arrow)" />'
        for source, target in WORKFLOW_EDGES
    )
    nodes: list[str] = []
    for task, center in zip(TASKS, centers, strict=True):
        status = statuses[task]
        trace = traces.get(task)
        duration = trace.get("duration_ms") if trace else (
            running_elapsed_ms if status == "running" else None
        )
        if status == "running" and trace is None:
            duration = running_elapsed_ms
        detail = f"{status} · {duration} ms" if duration is not None else status
        color = STATUS_COLORS.get(status, STATUS_COLORS["waiting"])
        nodes.append(
            f'<rect class="node" x="{center_x - 120}" y="{center - 34}" width="240" height="68" '
            f'rx="6" fill="{color}26" stroke="{color}" />'
            f'<text class="node-label" x="{center_x}" y="{center - 7}">{escape(task.capitalize())}</text>'
            f'<text class="node-meta" x="{center_x}" y="{center + 16}">{escape(detail)}</text>'
        )
    legend = " ".join(
        f'<span><i style="display:inline-block;width:9px;height:9px;border-radius:50%;'
        f'background:{color};margin-right:4px"></i>{label}</span>'
        for label, color in STATUS_COLORS.items()
        if label in {"waiting", "running", "success", "failed"}
    )
    return (
        f'<div class="workflow-topology"><svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="AI Workflow topology"><defs><marker id="workflow-arrow" markerWidth="8" '
        f'markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" '
        f'fill="currentColor" /></marker></defs>{edges}{"".join(nodes)}</svg>'
        f'<div style="display:flex;gap:14px;font-size:12px">{legend}</div></div>'
    )


def _view(
    state: WorkflowState,
    message: str | None = None,
    stopped: bool = False,
    running_elapsed_ms: int | None = None,
) -> tuple[Any, ...]:
    traces = {trace["task"]: trace for trace in state.get("traces", [])}
    failed = bool(state.get("error")) or stopped
    rows: list[list[Any]] = []
    panels: list[Any] = []
    active_assigned = False
    for task in TASKS:
        trace = traces.get(task)
        status = "waiting"
        node_event = state.get("node_events", {}).get(task, {})
        if task in state.get("node_events", {}):
            status = str(node_event["status"])
        elif trace:
            status = trace["status"]
        elif failed:
            status = "skipped"
        elif not active_assigned:
            status = "running"
            active_assigned = True
        rows.append([
            task,
            status,
            trace["model"] if trace else "",
            trace["duration_ms"] if trace else (
                running_elapsed_ms if status == "running" else None
            ),
            trace.get("usage", {}).get("total_tokens") if trace else None,
        ])
        metadata = {
            key: value for key, value in (trace or {}).items()
            if key not in ("input", "actual_output", "parsed_output", "output_schema")
        }
        metadata["status"] = status
        panels.extend([
            (trace or {}).get("input"),
            (trace or {}).get("parsed_output"),
            (trace or {}).get("actual_output"),
            metadata,
            (trace or {}).get("output_schema"),
        ])
    if message is None:
        if failed:
            message = state.get("error", "Workflow interrupted")
        elif len(traces) == len(TASKS):
            message = "Completed" if state.get("mode") == "llm" else "Completed (mock, no LLM calls)"
        else:
            message = "Running"
    run = state.get("run", {})
    attempts = [
        {"task": trace.get("task"), "attempts": trace.get("attempts", [])}
        for trace in state.get("traces", [])
    ]
    semantic = {
        "input": state.get("value"),
        "tasks": [
            {
                "task": trace.get("task"),
                "system_prompt": (trace.get("input") or [{}])[0].get("content"),
                "user_input": (trace.get("input") or [{}, {}])[1].get("content"),
                "parsed_output": trace.get("parsed_output"),
            }
            for trace in state.get("traces", [])
        ],
        "artifacts": state.get("artifacts", {}),
    }
    control = {
        "tasks": [
            {
                "task": trace.get("task"),
                "provider": trace.get("provider"),
                "model": trace.get("model"),
                "timeouts": {
                    "connect": trace.get("connect_timeout_seconds"),
                    "read": trace.get("read_timeout_seconds"),
                },
                "max_retries": trace.get("max_retries"),
                "retry_policy": trace.get("retry_policy"),
                "output_schema": trace.get("output_schema"),
                "usage": trace.get("usage", {}),
            }
            for trace in state.get("traces", [])
        ]
    }
    audit = {
        "run": run,
        "nodes": state.get("node_events", {}),
        "calls": [
            {
                "task": trace.get("task"),
                "input_hash": trace.get("input_hash"),
                "prompt_hash": trace.get("prompt_hash"),
                "schema_hash": trace.get("schema_hash"),
                "started_at": trace.get("call_started_at"),
                "finished_at": trace.get("finished_at"),
                "status": trace.get("status"),
                "error": trace.get("error"),
            }
            for trace in state.get("traces", [])
        ],
    }
    return (
        message,
        rows,
        *panels,
        run,
        attempts,
        state.get("validations", run.get("validation_results", [])),
        state.get("artifacts", {}),
        semantic,
        control,
        audit,
        dict(state),
        _workflow_topology(state, stopped, running_elapsed_ms),
    )


async def run_console(value: str, mode: WorkflowMode) -> AsyncIterator[tuple[Any, ...]]:
    state: WorkflowState = {"value": value, "mode": mode}
    if not value.strip():
        yield _view(state, "Please enter text", stopped=True)
        return
    # Reset every output before starting a new run so stale results cannot survive.
    yield _view(state, running_elapsed_ms=0)
    next_state: asyncio.Task[WorkflowState] | None = None
    try:
        states = aiter(stream_workflow_events(value, mode))
        next_state = asyncio.create_task(anext(states))
        stage_started = time.perf_counter()
        while True:
            done, _pending = await asyncio.wait({next_state}, timeout=0.25)
            if not done:
                elapsed = round((time.perf_counter() - stage_started) * 1000)
                yield _view(state, running_elapsed_ms=elapsed)
                continue
            try:
                state = next_state.result()
            except StopAsyncIteration:
                break
            yield _view(state)
            stage_started = time.perf_counter()
            next_state = asyncio.create_task(anext(states))
    except Exception as error:
        # Never expose arbitrary exception text that may include URLs or credentials.
        yield _view(state, f"{type(error).__name__}: Workflow interrupted", stopped=True)
    finally:
        if next_state is not None and not next_state.done():
            next_state.cancel()
            await asyncio.gather(next_state, return_exceptions=True)


def build_workflow_console() -> None:
    value = gr.Textbox(
        label="Input text", value=EXAMPLES["News"], lines=5, max_lines=12
    )
    with gr.Row(elem_classes=["workflow-examples"]):
        for label, sample in EXAMPLES.items():
            button = gr.Button(label, size="sm")
            button.click(
                lambda text=sample: text,
                inputs=None,
                outputs=value,
                queue=False,
                api_name=False,
            )
    with gr.Row():
        mode = gr.Radio(
            choices=["mock", "llm"],
            value=get_ai_settings().workflow_mode,
            label="Execution mode",
        )
        run = gr.Button("Run workflow", variant="primary")
    status = gr.Textbox(
        label="Workflow status",
        value="Ready",
        interactive=False,
        elem_classes=["workflow-status"],
    )
    topology = gr.HTML(
        label="Workflow topology",
        value=_workflow_topology({"value": "", "mode": "mock"}, active=False),
    )
    overview = gr.Dataframe(
        headers=["Task", "Status", "Model", "Duration (ms)", "Tokens"],
        datatype=["str", "str", "str", "number", "number"],
        row_count=(3, "fixed"),
        column_count=(5, "fixed"),
        value=[[task, "waiting", "", None, None] for task in TASKS],
        interactive=False,
        label="Calls",
    )
    # Keep component references in task order so streamed values map to the
    # existing console contract even though the UI is grouped into tabs.
    input_components: list[Any] = []
    parsed_components: list[Any] = []
    raw_components: list[Any] = []
    detail_components: list[Any] = []
    schema_components: list[Any] = []
    gr.Markdown("### LLM 输入 JSON")
    with gr.Tabs():
        for task in TASKS:
            with gr.Tab(task.capitalize()):
                input_components.append(
                    gr.JSON(label="Input messages (system / user)", open=True)
                )
    gr.Markdown("### LLM 解析输出 JSON")
    with gr.Tabs():
        for task in TASKS:
            with gr.Tab(task.capitalize()):
                parsed_components.append(gr.JSON(label="Parsed output", open=True))
    gr.Markdown("### 原始模型响应")
    with gr.Tabs():
        for task in TASKS:
            with gr.Tab(task.capitalize()):
                raw_components.append(gr.JSON(label="Content / tool calls", open=True))
    gr.Markdown("### 调用详情与 Output Schema")
    with gr.Tabs():
        for task in TASKS:
            with gr.Tab(task.capitalize()):
                detail_components.append(
                    gr.JSON(label="Provider / parameters / usage", open=True)
                )
                schema_components.append(gr.JSON(label="Output schema", open=False))
    outputs: list[Any] = [status, overview]
    for index in range(len(TASKS)):
        outputs.extend(
            [
                input_components[index],
                parsed_components[index],
                raw_components[index],
                detail_components[index],
                schema_components[index],
            ]
        )
    with gr.Accordion("Workflow state", open=False):
        aggregate = gr.JSON(label="Aggregate result", open=True)
    run_summary = gr.JSON(label="Global run summary", open=True)
    attempts = gr.JSON(label="LLM Task / Call / Attempt", open=True)
    validations = gr.JSON(label="Validation / Gate checks", open=True)
    artifacts = gr.JSON(label="Workflow artifacts", open=True)
    semantic = gr.JSON(label="Semantic", open=True)
    control = gr.JSON(label="Control", open=True)
    audit = gr.JSON(label="Audit", open=True)
    outputs.extend(
        [
            run_summary,
            attempts,
            validations,
            artifacts,
            semantic,
            control,
            audit,
            aggregate,
            topology,
        ]
    )
    run.click(
        run_console,
        inputs=[value, mode],
        outputs=outputs,
        concurrency_limit=1,
        concurrency_id="example-workflow",
        api_name=False,
    )
