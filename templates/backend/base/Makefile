.DEFAULT_GOAL := dev
.PHONY: dev dev-workflow dev-ai dev-identity dev-saas dev-full gradio lang-dev migrate check infra-up infra-down infra-status infra-reset smoke

export PYTHONPATH := $(CURDIR)/src$(if $(PYTHONPATH),:$(PYTHONPATH))
dev:
	uv sync {{SYNC_EXTRAS}}
	uv run python -m {{PACKAGE_NAME}}.dev dev
dev-workflow:
	uv sync {{SYNC_EXTRAS}}
	APP_PROFILE=workflow uv run python -m {{PACKAGE_NAME}}.dev dev
dev-ai:
	uv sync {{SYNC_EXTRAS}}
	APP_PROFILE=ai uv run python -m {{PACKAGE_NAME}}.dev dev
dev-identity:
	uv sync {{SYNC_EXTRAS}}
	APP_PROFILE=identity uv run python -m {{PACKAGE_NAME}}.dev dev
dev-saas:
	uv sync {{SYNC_EXTRAS}}
	APP_PROFILE=saas uv run python -m {{PACKAGE_NAME}}.dev dev
dev-full:
	uv sync {{SYNC_EXTRAS}}
	APP_PROFILE=full uv run python -m {{PACKAGE_NAME}}.dev dev
gradio:
	uv run python -m {{PACKAGE_NAME}}.gradio_app
lang-dev:
	uv run langgraph dev --config langgraph.json --host 127.0.0.1 --port 8123
migrate:
	uv run python -m alembic upgrade head
infra-up:
	uv run python -m {{PACKAGE_NAME}}.dev infra-up
infra-down:
	uv run python -m {{PACKAGE_NAME}}.dev infra-down
infra-status:
	uv run python -m {{PACKAGE_NAME}}.dev infra-status
infra-reset:
	uv run python -m {{PACKAGE_NAME}}.dev infra-reset
check:
	uv sync {{SYNC_EXTRAS}}
	uv run python -m ruff format .
	uv run python -m ruff check .
	uv run python -m mypy src
	uv run python -m pytest
smoke:
	uv run python -m {{PACKAGE_NAME}}.dev smoke
