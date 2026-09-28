# {{PROJECT_NAME}}

Generated backend project using the `{{PROFILE}}` profile.

```bash
cp .env.example .env
uv sync
make dev
```

Installed modules: `{{MODULES}}`.

The generated project uses Docker only for local PostgreSQL, Temporal, Mailpit and
MinIO when the selected modules require them. The API, Temporal worker, outbox
dispatcher and Gradio run from the host Python environment.

Infrastructure UIs, when installed:

- Temporal UI: `http://localhost:8233`
- Mailpit: `http://localhost:8025`
- MinIO Console: `http://localhost:9001`

When Temporal is installed, local development starts
`TEMPORAL_WORKER_PROCESSES` worker processes (default `1`). They all poll the same
`TEMPORAL_TASK_QUEUE`. `TEMPORAL_MAX_CONCURRENT_ACTIVITIES` (default `100`) limits
concurrent Activities per worker process, so approximate aggregate Activity capacity is
the product of these values. Size database pools and downstream provider limits for that
aggregate load. The Jobs global and tenant concurrency settings remain separate admission
controls.

This is a composition format v2 project. Runtime modules and SQLAlchemy models are
registered explicitly in `core/installed_modules.py` and `core/installed_models.py`.
Do not import another module's ORM classes from business code; use its public service
or protocol. Business services own commits, jobs are enqueued through the transactional
outbox, and API errors must keep the standard request-ID error envelope.

Use `APP_ENV=local|staging|production`. Local loads `.env` then `.env.local`;
deployed environments accept process environment and `/run/secrets` only.
Before production, replace all development credentials, set a unique
`AUTH_JWT_SECRET` of at least 32 characters when Auth is installed, disable local
namespace/bucket creation as appropriate, and point SMTP/S3/AI settings at managed
providers. Never commit `.env`.

The generated `.gitignore` excludes `.env` and `.env.*` while keeping the
credential-free `.env.example` trackable. Store real default and task-specific API
keys only in ignored environment files, deployment-platform secrets, or a secret
manager. Before committing, verify with `git check-ignore .env` and inspect
`git diff --cached`. The generated GitHub workflow also runs Gitleaks against Git
history. If a key is ever committed, revoke and rotate it immediately; removing the
file from a later commit does not remove it from Git history.

When the AI module is installed, `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`, and the
other `AI_*` values form the default LLM configuration. Connection and read
timeouts default to 20 and 300 seconds and can be overridden with
`AI_CONNECT_TIMEOUT_SECONDS` and `AI_READ_TIMEOUT_SECONDS`. Add task-specific partial
overrides as JSON; every omitted value inherits from those defaults:

```dotenv
AI_TASK_CONFIGS={"summarize":{"model":"gpt-5-mini","temperature":0.2},"extract":{"base_url":"https://llm.example/v1","api_key":"task-key","model":"extract-model"}}
```

Individual values can also use nested environment variables, which are convenient
for deployment secret/config stores:

```dotenv
AI_TASK_CONFIGS__SUMMARIZE__MODEL=gpt-5-mini
AI_TASK_CONFIGS__SUMMARIZE__TEMPERATURE=0.2
```

Select a configuration with `create_chat_model(task="summarize")` or pass the same
`task` argument to `structured_output`. Unknown task names use the default configuration.

The generated AI profile includes a three-task LangGraph workflow: `summarize`,
`extract`, and `classify`. Gradio defaults to `AI_WORKFLOW_MODE=mock`, which is
offline and credential-free; choose `llm` to call the configured provider. Each LLM
task starts with an explicit system message that assigns the model the corresponding
summarization, structured extraction, or text-classification expert role. All three
tasks use `json_mode`; each task's explicit `output_schema` is included in the actual
system message and validates the parsed response.
The Gradio console includes multiple input presets and separate task panels showing
the actual system/user messages, raw model response, parsed output, schema, provider
settings, latency, token usage, and execution status for every LLM call. Input JSON and
parsed output JSON are each grouped into tabs for `summarize`, `extract`, and `classify`.
Its topology is generated from the LangGraph stage and edge definitions, using rectangles
for nodes and arrows for edges. Streaming events and a 250 ms running heartbeat update
waiting, running, success, and failure states together with elapsed time.
The console also exposes the global Run summary, LLM Task/Call/Attempt retry records,
input/prompt/schema fingerprints, validation checks, and extensible workflow artifacts.
Semantic, control, and audit information are separated, while API keys, authorization
values, and provider headers are redacted.

For parallel local projects, assign unique `APP_SLUG`, database name, host ports,
storage bucket, Temporal namespace and task queue. `make infra-reset` deletes only the
current Compose project's volumes and must be invoked explicitly.

The jobs module provides versioned job types, DLQ/redrive, priority and concurrency
admission, tenant quotas, batches, timezone-aware schedules, and signed webhooks. The
storage module provides multipart resume, immutable revisions, media inspection,
lifecycle cleanup, S3 event ingestion, and private CDN download signing.
