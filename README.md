# Full-stack Foundation

面向同一套成熟 Python 技术栈的可组合全栈底座与项目生成器。每次生成一个独立的
`frontend/` Next.js 工程和 `backend/` FastAPI 工程；可靠任务、身份认证、对象存储与 AI
能力按 profile 组合。

生成结果可分别运行：

```bash
cd frontend && pnpm install && pnpm dev
cd backend && make dev
```

## 快速开始

安装 Python 3.12、uv、Docker Desktop/Engine 和 Make：

```bash
cp .env.example .env
make dev
```

默认 API 地址为 `http://127.0.0.1:8000`：

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
make smoke
make check
```

Docker 仅负责本地 PostgreSQL、Temporal、Temporal UI、Mailpit 和 MinIO；API、Worker、
outbox dispatcher、Gradio 和 LangGraph Studio 仍由宿主机的 uv 启动。Compose project、
volume、数据库、bucket、Temporal namespace、task queue 和端口均可按项目隔离。

## Profiles

| Profile | 模块 | 启动命令 |
|---|---|---|
| `api` | Database | `make dev` |
| `workflow` | Database + Temporal + durable Jobs | `make dev-workflow` |
| `ai` | Database + LangGraph + Gradio | `make dev-ai` |
| `identity` | Workflow + Users + RBAC + Email + Auth | `make dev-identity` |
| `saas` | Identity + private S3-compatible Storage | `make dev-saas` |
| `full` | SaaS + AI provider/LangGraph/Gradio | `make dev-full` |

单独运行 AI 工具：

```bash
make lang-dev
make gradio
```

`make gradio` 默认打开“全栈项目生成器”页面，无需输入 CLI 命令。填写项目名称、选择
六个内置 profile 后，即可生成并下载独立 ZIP。高级选项可显式组合模块，依赖会自动补齐；
如果最终组合不属于内置 profile，生成项目会使用 `custom` profile。LangGraph smoke 调试保留在
“AI 调试台”标签页。

下载包仅由仓库内的版本化模板生成，不复制当前工作树，并排除 `.git`、`.venv`、缓存、临时
目录和本地运行数据；会保留前后端各自的本地 `.env`，方便下载后直接联调。服务器端下载
文件会定期清理。下载后的常规启动方式为：

```bash
unzip my-service.zip
cd my-service
cd frontend && pnpm install && cd ..
make dev
```

实际运行时直接修改 `DATABASE_URL`、`TEMPORAL_HOST`、`API_PORT` 等环境变量即可。

基础设施命令：

```bash
make infra-up
make infra-status
make infra-down
make infra-reset  # 显式删除当前项目 volume
```

`make dev` 会自动启动当前 profile 所需的基础设施；Ctrl-C 只停止宿主机应用进程，
不会自动停止 Docker 基础设施。workflow/full profile 的 Temporal UI 默认地址为
`http://localhost:8233`。identity 以上 profile 的 Mailpit UI 默认为
`http://localhost:8025`，saas/full 的 MinIO Console 默认为 `http://localhost:9001`。

Temporal profile 默认启动一个 Worker 进程。可通过 `TEMPORAL_WORKER_PROCESSES` 启动多个
进程，它们共同轮询同一个 `TEMPORAL_TASK_QUEUE`；`TEMPORAL_MAX_CONCURRENT_ACTIVITIES` 设置
每个 Worker 进程允许并发执行的 Activity 数。因而理论 Activity 总容量约为两者乘积，例如
`3 × 20 = 60`。扩容时应同步核对数据库连接池、LLM/HTTP Provider 限流和其他下游容量。
这两个配置不替代 `JOBS_GLOBAL_CONCURRENCY` 与 `JOBS_TENANT_CONCURRENCY` 的任务准入限制。

## 通用模块

- `jobs` 使用数据库唯一约束、transactional outbox 与 Temporal 实现幂等任务、版本化类型、
  指数退避、DLQ、优先级、并发 lease、租户配额、批量任务和带时区 Schedule。租户管理 API
  位于 `/organizations/{organization_id}/jobs`，平台运维 API 位于 `/admin/jobs`。
- `users/rbac/auth` 提供组织、membership、自定义角色、`resource:action` 权限、Argon2id、
  短期 access JWT、refresh rotation/reuse detection、CSRF/Origin 校验和会话撤销。
- `email` 只发送预注册模板，并通过 `email.send` Job 投递。Mailpit 仅是本地开发收件箱。
- `storage` 使用私有 S3-compatible bucket，支持单次/分片预签名上传、断点续传、应用级文件
  版本、媒体元数据与隔离、生命周期清理、签名 S3 事件、私有 CDN 下载。MinIO 仅是本地 provider。
- `ai` 提供 lazy OpenAI-compatible ChatModel factory、timeout/retry 和一个可在 Gradio 中运行的
  三任务 LangGraph（`summarize`、`extract`、`classify`）。默认 `mock` 模式无需 API Key 即可离线
  测试；切换为 `llm` 后按任务调用真实模型。每个 LLM 节点均使用显式 system message，分别将
  模型设定为摘要、结构化信息抽取或文本分类专家。三个任务统一使用 `json_mode`，各自的显式
  `output_schema` 会加入实际 system message 并用于响应校验；不会记录 key 或敏感 Provider header。

AI 模块以 `AI_BASE_URL`、`AI_API_KEY`、`AI_MODEL`、`AI_CONNECT_TIMEOUT_SECONDS`、
`AI_READ_TIMEOUT_SECONDS`、`AI_TEMPERATURE`、`AI_MAX_TOKENS` 等作为默认配置；连接超时默认
为 20 秒，读取超时默认为 300 秒，均可通过环境变量覆盖。通过 `AI_TASK_CONFIGS` JSON 可为不同任务只覆盖所需字段，
未覆盖字段自动继承默认值，未配置的任务则完整使用默认配置：

```dotenv
AI_TASK_CONFIGS={"summarize":{"model":"gpt-5-mini","temperature":0.2},"extract":{"base_url":"https://llm.example/v1","api_key":"task-key","model":"extract-model"}}
```

通过 `AI_WORKFLOW_MODE=mock|llm` 设置默认运行模式；Gradio 也可在界面中切换。
调试台提供新闻、问题、请求、投诉、技术报告和混合意图示例按钮，并为三个任务分别展示
实际 system/user 输入、原始模型响应、结构化结果、模型与 Provider、耗时、token 用量和执行状态。
输入 JSON 与解析输出 JSON 分别按 `summarize`、`extract`、`classify` 使用标签页切换查看。
拓扑图直接读取 LangGraph 的阶段和边定义，以矩形表示节点、箭头表示边；执行期间按流式事件
和 250ms 运行心跳更新节点的等待、运行、成功、失败状态及耗时。
调试台同时提供全局 Run 摘要、LLM Task/Call/Attempt 重试记录、输入/Prompt/Schema 指纹、
Validation/Gate Checks 和可扩展 Workflow artifacts；Semantic、Control、Audit 信息分层展示，
敏感 key、Authorization 和 Provider headers 会被脱敏。

部署平台也可使用分层环境变量，例如
`AI_TASK_CONFIGS__SUMMARIZE__MODEL=gpt-5-mini`。代码中通过
`create_chat_model(task="summarize")` 或 `structured_output(..., task="summarize")` 选择任务。

## 架构边界

```text
core -> 定义应用工厂、配置、错误协议、日志、健康检查和模块契约
modules -> 实现可选能力，只通过公开契约依赖其他模块
app.py -> 显式选择并组合模块，是唯一生产组合根
```

模块使用 `ModuleSpec` v2 声明依赖、Router、lifespan、readiness、settings、模型、迁移、
权限、job handler、Temporal workflow/activity、环境变量、可选依赖和 Compose capability。
模块由组合根显式注册，不进行目录扫描或隐式发现。

新增模块时必须遵守：

- 模块不得直接引用其他模块的 ORM 实现；跨模块交互使用公开 Service/Protocol。
- 所有 ORM 模型继承统一的 `Base`，使 Alembic 能聚合 metadata。
- 数据库事务由业务服务显式提交；请求异常由 session dependency rollback。
- API 错误必须使用统一错误协议，并保留 `X-Request-ID`。
- 新的外部依赖必须提供 lifespan 清理、readiness check 和失败测试。
- Workflow/activity/job handler 必须通过 `ModuleSpec` 注册；handler 固定接收 `job_id + payload`。
- 敏感配置必须使用 `SecretStr`，不得进入日志、错误响应、模板 ZIP 或版本控制。

## 配置

主要配置参见 `.env.example`。`APP_ENV` 只接受 `local`、`staging`、`production`；旧值
`development` 会迁移为 `local`。local 按 `.env`、`.env.local` 加载，环境变量优先；
staging/production 不读取 dotenv，凭据由进程环境或 `/run/secrets` 注入：

- `APP_NAME`、`APP_SLUG`、`APP_VERSION`、`APP_ENV`
- `APP_PROFILE=api|workflow|ai|identity|saas|full`
- `DATABASE_URL` 及 PostgreSQL 容器连接配置
- Temporal host、namespace、task queue 和端口
- `TEMPORAL_UI_URL`、`TEMPORAL_AUTO_REGISTER_NAMESPACE`
- Auth issuer/audience/secret、允许 Origin 和 Secure Cookie 策略
- Mailpit SMTP/UI、MinIO API/Console、bucket、AI provider 与 API/Gradio 地址

生产环境必须设置唯一且至少 32 字符的 `AUTH_JWT_SECRET`；`backend doctor` 会拒绝默认或
过短的生产 secret。生成项目会忽略 `.env` 和 `.env.*`，只允许提交不含真实凭据的
`.env.example`；生成 ZIP 只包含开发用默认 `.env`，不包含当前工作树的环境文件。CI 使用
Gitleaks 扫描受跟踪文件和 Git 历史。
真实的默认及任务级 API Key 只能写入被忽略的环境文件、部署平台 Secret 或密钥管理服务，
不得写入 `.env.example`、README、源码或测试数据。提交前可用 `git check-ignore .env` 确认
本地配置确实被忽略，并用 `git diff --cached` 检查暂存内容。

staging/production 还要求 Secure Cookie、HTTPS Origin、预创建 bucket 和非默认存储凭据。
可用 `backend doctor --env staging|production` 检查对应部署配置。Webhook secret 只在创建时
返回一次，`JOBS_WEBHOOK_ENCRYPTION_KEY` 必须是 URL-safe base64 编码的 32 字节密钥。

分片上传入口为 `/organizations/{organization_id}/files/upload-sessions`；服务端以对象存储
`ListParts` 结果完成上传，不信任客户端自行声明。媒体文件在异步安全处理完成前不可下载。
启用队列事件时单独运行 `python -m backend_foundation.modules.storage.event_consumer`。
平台 Job 管理员通过 `backend platform-admin grant|revoke <email>` 管理。

生产环境使用 JSON 日志。所有请求响应都会携带 `X-Request-ID`，错误响应使用统一
`error.code/message/request_id/details` 结构。

## 迁移说明

第三阶段使用 composition format v2，不兼容第二阶段已经生成的项目。每个模块携带版本化
迁移模板，生成器按依赖顺序构造线性单 head，并通过 `installed_models.py` 显式聚合 metadata。
若本地数据库记录过旧 revision，请为开发环境显式执行 `make infra-reset`，或准备新的空库；
底座不会自动删除外部数据库。

## 依赖安装

生产安装可按能力选择：

```bash
uv sync --no-dev
uv sync --no-dev --extra workflow
uv sync --no-dev --extra ai
uv sync --no-dev --extra identity
uv sync --no-dev --extra saas
uv sync --no-dev --extra full
```

开发依赖包含所有 profile 所需库，以便执行完整测试矩阵。生产环境只有启用相应
extra 时才会安装 Temporal 或 LangGraph。

## 生成与组合

仓库内置 `backend` CLI 和版本化模板。它使用标准库 `tomllib` 读取
`module.toml`，不会扫描当前工作树、访问网络或自动启动 Docker：

```bash
uv run backend new my-service --profile api --output /tmp/my-service
cd /tmp/my-service
cd frontend && pnpm install && cd ..
make dev
```

可用 profile 为 `api`、`workflow`、`ai`、`identity`、`saas` 和 `full`。生成项目的 Python
包名由项目 slug 转换为 snake_case，并在 `core/installed_modules.py` 与
`core/installed_models.py` 中显式记录组合结果。生成后的能力组合固定，不再提供模块追加命令。

`doctor` 从全栈根目录检查两个子项目、API/Origin 环境契约，再委托后端检查 composition v2、
manifest/模块/模型注册表、迁移覆盖与单 head、extras、环境变量、安全配置、端口、Compose
capability 和 Docker 可用性；ERROR 返回非零退出码，WARN 不阻断命令。

多个项目并行运行时，为每个项目设置不同的 `APP_SLUG`、数据库名、host ports、bucket、
Temporal namespace 和 task queue。`infra-down/reset` 只作用于当前 Compose project；
`infra-reset` 会删除当前项目 volume，必须显式调用。

模块安装元数据位于 `templates/backend/modules/*/module.toml`，profile 位于
`templates/profiles/*/profile.toml`。安装阶段使用 manifest，运行阶段使用生成的显式
模块清单，运行时行为仍由 `ModuleSpec` 契约负责。新增模块需要同时提供 manifest、模板
文件、依赖声明和测试，并保持 core -> modules -> app 的依赖方向。
