import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import BaseModel

from backend_foundation.modules.ai.provider import (
    create_chat_model,
    invoke_structured_model,
    structured_output,
)
from backend_foundation.modules.ai.settings import AISettings, LLMTaskConfig


def test_ai_provider_is_lazy_and_requires_key() -> None:
    settings = AISettings(_env_file=None, api_key="")
    with pytest.raises(RuntimeError, match="AI_API_KEY"):
        create_chat_model(settings)


def test_ai_secret_is_not_exposed() -> None:
    settings = AISettings(_env_file=None, api_key="private-ai-key")
    assert "private-ai-key" not in repr(settings)


def test_task_config_overrides_only_explicit_values() -> None:
    settings = AISettings(
        _env_file=None,
        base_url="https://default.example/v1",
        api_key="default-secret",
        model="default-model",
        temperature=0.2,
        max_tokens=4096,
        task_configs={
            "Summarize": LLMTaskConfig(
                base_url="https://fast.example/v1",
                model="summary-model",
                temperature=0.7,
            )
        },
    )

    summary = settings.for_task(" summarize ")
    assert summary.base_url == "https://fast.example/v1"
    assert summary.model == "summary-model"
    assert summary.temperature == 0.7
    assert summary.max_tokens == 4096
    assert summary.api_key.get_secret_value() == "default-secret"

    fallback = settings.for_task("unconfigured-task")
    assert fallback.base_url == "https://default.example/v1"
    assert fallback.model == "default-model"
    assert fallback.temperature == 0.2


def test_task_config_loads_from_json_and_nested_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AI_TASK_CONFIGS",
        '{"translate":{"base_url":"https://translate.example/v1","temperature":0.1}}',
    )
    monkeypatch.setenv("AI_TASK_CONFIGS__TRANSLATE__MODEL", "translation-model")
    monkeypatch.setenv("AI_TASK_CONFIGS__EXTRACT__MODEL", "extraction-model")
    settings = AISettings(
        _env_file=None,
        api_key="shared-secret",
        model="default-model",
        max_tokens=2048,
    )

    translate = settings.for_task("TRANSLATE")
    assert translate.base_url == "https://translate.example/v1"
    assert translate.model == "translation-model"
    assert translate.temperature == 0.1
    assert translate.max_tokens == 2048
    assert translate.api_key.get_secret_value() == "shared-secret"
    assert settings.for_task("extract").model == "extraction-model"


def test_create_chat_model_uses_task_config() -> None:
    settings = AISettings(
        _env_file=None,
        api_key="shared-secret",
        model="default-model",
        task_configs={
            "reasoning": {
                "api_key": "task-secret",
                "model": "reasoning-model",
                "temperature": 0.4,
                "top_p": 0.8,
                "reasoning_effort": "high",
                "extra_body": {"provider": {"order": ["example"]}},
            }
        },
    )

    model = create_chat_model(settings, task="reasoning")

    assert model.model_name == "reasoning-model"
    assert model.temperature == 0.4
    assert model.top_p == 0.8
    assert model.reasoning_effort == "high"
    assert model.openai_api_key.get_secret_value() == "task-secret"
    assert model.extra_body == {"provider": {"order": ["example"]}}


class Answer(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_structured_invocation_records_real_attempts_without_secrets() -> None:
    class FakeStructured:
        calls = 0

        async def ainvoke(self, _messages: object) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider authorization header secret")
            return {
                "raw": type(
                    "Raw",
                    (),
                    {
                        "model_dump": lambda self, **_kwargs: {"content": '{"answer":"ok"}'},
                    },
                )(),
                "parsed": Answer(answer="ok"),
                "parsing_error": None,
            }

    class FakeModel:
        def __init__(self) -> None:
            self.structured = FakeStructured()

        def with_structured_output(self, _schema: object, **_kwargs: object) -> FakeStructured:
            return self.structured

    result = await invoke_structured_model(
        FakeModel(),
        Answer,
        [{"role": "user", "content": "hello"}],
        max_retries=1,
    )
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["status"] == "failed"
    assert result["attempts"][1]["status"] == "success"
    assert result["attempts"][0]["retry_reason"]
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_openai_compatible_retry_structured_output_and_timeout() -> None:
    class Handler(BaseHTTPRequestHandler):
        attempts = 0

        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            Handler.attempts += 1
            if Handler.attempts == 1:
                self.send_response(500)
                self.end_headers()
                return
            if any("timeout-case" in str(item) for item in body.get("messages", [])):
                time.sleep(0.3)
            content = '{"answer":"ok"}' if body.get("response_format") else "ok"
            response = {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "fake",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        settings = AISettings(
            _env_file=None,
            base_url=base_url,
            api_key="fake-secret",
            model="fake",
            max_retries=2,
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
        )
        result = await structured_output(
            Answer, [{"role": "user", "content": "structured"}], settings
        )
        assert result.answer == "ok"
        assert Handler.attempts == 2
        timeout = AISettings(
            _env_file=None,
            base_url=base_url,
            api_key="fake-secret",
            model="fake",
            max_retries=0,
            connect_timeout_seconds=1,
            read_timeout_seconds=0.05,
        )
        with pytest.raises(Exception) as error:
            await create_chat_model(timeout).ainvoke("timeout-case")
        assert "fake-secret" not in str(error.value)
    finally:
        server.shutdown()
        thread.join(timeout=2)
