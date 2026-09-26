from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from pydantic import BaseModel

from backend_foundation.modules.ai.provider import create_chat_model, structured_output
from backend_foundation.modules.ai.settings import AISettings

from .support import IntegrationProject

pytestmark = [pytest.mark.integration, pytest.mark.full]


class Answer(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_full_runtime_and_fake_openai_provider(full_runtime: IntegrationProject) -> None:
    expected_processes = {"api", "worker", "dispatcher", "gradio"}
    assert {process.name for process in full_runtime.processes} == expected_processes
    for process in full_runtime.processes:
        process.assert_running()
    api = f"http://127.0.0.1:{full_runtime.env['API_PORT']}"
    assert httpx.get(f"{api}/health/live").status_code == 200
    assert httpx.get(f"{api}/health/ready").status_code == 200

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
            payload = json.dumps(
                {
                    "id": "chatcmpl-integration",
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
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        settings = AISettings(
            _env_file=None,
            base_url=base_url,
            api_key="fake-integration-secret",
            model="fake",
            max_retries=2,
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
            api_key="fake-integration-secret",
            model="fake",
            max_retries=0,
            read_timeout_seconds=0.05,
        )
        with pytest.raises(Exception) as error:
            await create_chat_model(timeout).ainvoke("timeout-case")
        assert "fake-integration-secret" not in str(error.value)
    finally:
        server.shutdown()
        thread.join(timeout=2)
