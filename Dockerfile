FROM python:3.12.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
WORKDIR /app

ARG APP_EXTRA=""
COPY pyproject.toml uv.lock ./
RUN if [ -n "$APP_EXTRA" ]; then \
      uv sync --frozen --no-dev --no-install-project --extra "$APP_EXTRA"; \
    else \
      uv sync --frozen --no-dev --no-install-project; \
    fi

COPY src ./src
COPY templates ./templates
RUN if [ -n "$APP_EXTRA" ]; then \
      uv sync --frozen --no-dev --extra "$APP_EXTRA"; \
    else \
      uv sync --frozen --no-dev; \
    fi

FROM python:3.12.13-slim
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/templates /app/templates
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
CMD ["uvicorn", "backend_foundation.app:app", "--host", "0.0.0.0", "--port", "8000"]
