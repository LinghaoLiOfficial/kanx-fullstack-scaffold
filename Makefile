SHELL := /bin/bash
.DEFAULT_GOAL := dev
.PHONY: dev dev-workflow dev-ai dev-identity dev-saas dev-full gradio lang-dev infra-up infra-down infra-status infra-reset migrate smoke test lint typecheck check format integration integration-profiles integration-jobs integration-identity integration-saas integration-full integration-isolation integration-phase4
dev:
	uv sync --frozen
	APP_PROFILE=api uv run --no-sync python -m backend_foundation.dev dev
dev-workflow:
	uv sync --frozen --extra workflow
	APP_PROFILE=workflow uv run --no-sync python -m backend_foundation.dev dev
dev-ai:
	uv sync --frozen --extra ai
	APP_PROFILE=ai uv run --no-sync python -m backend_foundation.dev dev
dev-identity:
	uv sync --frozen --extra identity
	APP_PROFILE=identity uv run --no-sync python -m backend_foundation.dev dev
dev-saas:
	uv sync --frozen --extra saas
	APP_PROFILE=saas uv run --no-sync python -m backend_foundation.dev dev
dev-full:
	uv sync --frozen --extra full
	APP_PROFILE=full uv run --no-sync python -m backend_foundation.dev dev
gradio:
	uv sync --frozen --extra ai
	APP_PROFILE=ai GRADIO_ANALYTICS_ENABLED=false uv run --no-sync python -m backend_foundation.gradio_app
lang-dev:
	uv sync --frozen --extra ai
	LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false uv run --no-sync langgraph dev --config langgraph.json --host 127.0.0.1 --port 8123
infra-up:
	uv run python -m backend_foundation.dev infra-up
infra-down:
	uv run python -m backend_foundation.dev infra-down
infra-status:
	uv run python -m backend_foundation.dev infra-status
infra-reset:
	uv run python -m backend_foundation.dev infra-reset
migrate:
	uv run alembic upgrade head
smoke:
	uv run python -m backend_foundation.dev smoke
test:
	uv run pytest --cov=backend_foundation --cov-report=term-missing --cov-fail-under=85
lint:
	uv run ruff format --check .
	uv run ruff check .
typecheck:
	uv run mypy src
check: lint typecheck test
format:
	uv run ruff format .
integration:
	BACKEND_RUN_INTEGRATION=1 uv run pytest -m integration tests/integration -vv -s --durations=0
integration-profiles:
	BACKEND_RUN_INTEGRATION=1 uv run pytest -m "integration and profiles" tests/integration -vv -s --durations=0
integration-jobs:
	BACKEND_RUN_INTEGRATION=1 uv run pytest -m "integration and jobs" tests/integration -vv -s --durations=0
integration-identity:
	BACKEND_RUN_INTEGRATION=1 uv run pytest -m "integration and identity" tests/integration -vv -s --durations=0
integration-saas:
	BACKEND_RUN_INTEGRATION=1 uv run pytest -m "integration and saas" tests/integration -vv -s --durations=0
integration-full:
	BACKEND_RUN_INTEGRATION=1 uv run pytest -m "integration and full" tests/integration -vv -s --durations=0
integration-isolation:
	BACKEND_RUN_INTEGRATION=1 uv run pytest -m "integration and isolation" tests/integration -vv -s --durations=0
integration-phase4:
	BACKEND_RUN_INTEGRATION=1 uv run pytest -m integration tests/integration/test_phase4.py -vv -s --durations=0
