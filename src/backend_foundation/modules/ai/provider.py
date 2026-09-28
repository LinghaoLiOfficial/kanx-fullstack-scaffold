from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from .settings import AISettings, LLMConfig, get_ai_settings


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def content_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def safe_error(error: Exception) -> str:
    if isinstance(error, RuntimeError) and str(error).startswith("AI_API_KEY is required"):
        return "AI_API_KEY is required to create a chat model"
    return f"{type(error).__name__}: LLM call failed"


class StructuredInvocationError(RuntimeError):
    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


def create_chat_model(settings: AISettings | None = None, *, task: str | None = None) -> ChatOpenAI:
    configured = (settings or get_ai_settings()).for_task(task)
    if not configured.api_key.get_secret_value():
        raise RuntimeError("AI_API_KEY is required to create a chat model")
    optional_parameters = _optional_parameters(configured)
    return ChatOpenAI(
        model=configured.model,
        api_key=configured.api_key,
        base_url=configured.base_url,
        timeout=(configured.connect_timeout_seconds, configured.read_timeout_seconds),
        # Retries are explicit in invoke_structured_model so each attempt is observable.
        max_retries=0,
        temperature=configured.temperature,
        max_completion_tokens=configured.max_tokens,
        **optional_parameters,
    )


async def invoke_structured_model(
    model: Any,
    output_schema: type[BaseModel],
    messages: list[Any],
    *,
    max_retries: int,
) -> dict[str, Any]:
    """Invoke a JSON-mode model while retaining every real request attempt."""
    attempts: list[dict[str, Any]] = []
    structured = model.with_structured_output(
        output_schema, method="json_mode", include_raw=True
    )
    for attempt_number in range(1, max_retries + 2):
        started = time.perf_counter()
        started_at = utc_now()
        record: dict[str, Any] = {
            "attempt_number": attempt_number,
            "started_at": started_at,
            "finished_at": None,
            "duration_ms": None,
            "status": "running",
            "raw_output": None,
            "parsed_output": None,
            "error": None,
            "retry_reason": None,
        }
        try:
            response = await structured.ainvoke(messages)
            raw = response["raw"] if isinstance(response, dict) and "raw" in response else response
            record["raw_output"] = raw.model_dump(
                mode="json", include={"content", "tool_calls", "invalid_tool_calls"}
            ) if hasattr(raw, "model_dump") else str(raw)
            parsing_error = response.get("parsing_error") if isinstance(response, dict) else None
            if parsing_error:
                raise parsing_error
            parsed = response.get("parsed") if isinstance(response, dict) else response
            normalized = output_schema.model_validate(parsed).model_dump(mode="json")
            record["parsed_output"] = normalized
            record["status"] = "success"
            record["finished_at"] = utc_now()
            record["duration_ms"] = round((time.perf_counter() - started) * 1000)
            attempts.append(record)
            return {"parsed": normalized, "raw": raw, "attempts": attempts}
        except Exception as error:
            record["status"] = "failed"
            record["error"] = safe_error(error)
            record["finished_at"] = utc_now()
            record["duration_ms"] = round((time.perf_counter() - started) * 1000)
            if attempt_number <= max_retries:
                record["retry_reason"] = record["error"]
            attempts.append(record)
            if attempt_number > max_retries:
                raise StructuredInvocationError(record["error"], attempts) from error
            await asyncio.sleep(min(0.25, 0.05 * (2 ** (attempt_number - 1))))
    raise RuntimeError("Structured LLM call exhausted retries")


def _optional_parameters(configured: LLMConfig) -> dict[str, Any]:
    names = (
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "seed",
        "stop_sequences",
        "reasoning_effort",
        "verbosity",
        "service_tier",
        "organization",
    )
    parameters = {name: value for name in names if (value := getattr(configured, name)) is not None}
    for name in ("default_headers", "default_query", "extra_body", "model_kwargs"):
        if value := getattr(configured, name):
            parameters[name] = value
    return parameters


async def structured_output[Schema: BaseModel](
    output_schema: type[Schema],
    messages: list[Any],
    settings: AISettings | None = None,
    *,
    task: str | None = None,
) -> Schema:
    configured = (settings or get_ai_settings()).for_task(task)
    result = await invoke_structured_model(
        create_chat_model(settings, task=task),
        output_schema,
        messages,
        max_retries=configured.max_retries,
    )
    return output_schema.model_validate(result["parsed"])
