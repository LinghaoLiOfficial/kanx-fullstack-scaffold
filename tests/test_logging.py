import json
import logging

from backend_foundation.core.config import Settings
from backend_foundation.core.logging import ContextFilter, JsonFormatter, configure_logging


def test_json_formatter() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["message"] == "hello"


def test_configure_development_logging() -> None:
    configure_logging(Settings(_env_file=None))
    assert logging.getLogger().level == logging.INFO


def test_context_filter_adds_application_fields() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    assert ContextFilter(Settings(_env_file=None)).filter(record)
    assert record.app_name == "backend-foundation"
    assert record.request_id == "-"
