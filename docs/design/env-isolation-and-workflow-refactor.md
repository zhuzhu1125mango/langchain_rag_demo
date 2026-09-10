# 开发/生产环境隔离与工作流合规化改造（P0+P1）

> 状态：已实施（2026-09-08，P0+P1 全部完成；dev/prod 双栈并存验证通过，CI 已接入 env-consistency 校验）

## Context

当前"隔离"实为"切换覆盖"：`start-dev/prod.ps1|.sh` 把 `.env.dev`/`.env.prod` 复制覆盖到根 `.env`；两份 compose 服务名相同且 compose project 同名（目录名），导致一边 up 会移除重建另一边容器（postgres-prod 曾被移除）；端口完全重叠无法并存；dev 用 `latest` 镜像与 prod 固定 tag 漂移；三份 env 人工同步已发生漂移（`.env` 比 `.env.dev` 多 `OLLAMA_SUPPORTS_THINKING`、`KB_RERANK_ENABLED` 等 8 个在用键；`.env.example` 缺 14 个在用键含 `SECRET_KEY`）；`.env.backup.*` 不在 .gitignore（密钥泄漏风险）；Git 仅 main 直推、无 tag。

用户已确认：P0+P1 全做；本地裸跑支持 `APP_ENV` 切换。

## 前置处置（动手前确认）

1. 工作区现有未提交的 Wiki Phase 1/2 变更（10+ 修改、11+ 新文件）——**先按既有批次提交或用户指示处置**，避免与本改造混杂。
2. 根 `.env` 中独有键先合并回 `.env.dev`（修复漂移），**随后删除根 `.env`**（内容已确认无独立价值，属漂移产物）。

## 变更清单

### 1. `backend/src/config.py` — APP_ENV 支持
```python
APP_ENV = os.getenv("APP_ENV", "dev")          # dev | prod
ENV_FILE = os.path.join(PROJECT_ROOT, f".env.{APP_ENV}")
```
容器内 `.env*` 被 dockerignore 排除、compose 显式 env 变量优先级更高，行为不变；本地裸跑默认读 `.env.dev`。

### 2. 两份 compose 重构（核心）

**共同**：顶部加 `name: rag-dev` / `name: rag-prod`（compose project 隔离，容器互不重建）。

**backend 服务 environment 列表（~70 行）替换为 env_file 注入**：
```yaml
env_file:
  - .env.dev        # prod 为 .env.prod
environment:        # 仅保留 docker 网络路由覆盖（优先级高于 env_file）
  IN_DOCKER: "true"
  POSTGRES_HOST: postgres
  POSTGRES_PORT: "5432"
  MINIO_ENDPOINT: minio:9000
  MILVUS_HOST: milvus-standalone
  MILVUS_PORT: "19530"
  REDIS_HOST: redis
  REDIS_PORT: "6379"
  SEARXNG_BASE_URL: http://searxng:8080
  OLLAMA_HOST: host.docker.internal
  SKIP_OLLAMA_CHECK: "true"
  NLTK_DATA: /app/data/nltk_data
  # prod 额外补齐（修复漂移）：HF_HOME=/app/data/model_cache/huggingface、HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE
```
新增功能开关今后只改 env 文件，**不再改 compose**——根治历次漏传。

**其余服务**（redis/postgres/minio/searxng/milvus/grafana/postgres-exporter）继续 `${VAR}` 插值，由 `--env-file` 提供。

**prod 端口错开**（全部保持 127.0.0.1 绑定，frontend 80 除外）：

| 服务 | dev | prod 改为 |
|---|---|---|
| frontend / backend | 5173 / 8000 | 80 / **8001** |
| postgres / redis | 5433 / 6379 | **5434 / 6380** |
| minio API/控制台 | 9000/9001 | **9002/9003** |
| milvus gRPC/metrics | 19530/9091 | **19531/9092** |
| searxng | 8080 | **8081** |
| prometheus / grafana / alertmanager | 9090 / 3000 / — | **9094 / 3001 / 9095** |

同步 `.env.prod` 的 `POSTGRES_PORT=5434`（本地裸跑/alembic 用）。

**dev 镜像 tag 固定**（与 prod 对齐）：searxng `2026.8.20-8d3dd0cd4`、minio `RELEASE.2025-09-07T16-13-09Z`、postgres-exporter `v0.20.1`。

### 3. 启动脚本改造（4 处：start-dev/prod 的 ps1 + sh；.bat 仅包装无需改）
- 删除 `Copy-EnvFile` 函数及调用；`Load-EnvVars` 改读各自 env 文件
- compose 命令加 `--env-file .env.dev`（prod 同理）
- **start-prod 新增 Alembic 迁移步骤**：服务健康后 `docker compose -f docker-compose.yml exec -T backend alembic upgrade head`（alembic 为主依赖，prod 镜像可用；容器内 POSTGRES_HOST=postgres 直连）
- stop/logs 脚本引用路径不变，无需改

### 4. `.gitignore` — 补 `.env.backup.*`

### 5. `.env.example` 补齐 14 个缺失键
`SECRET_KEY`、`OLLAMA_DIRECT_MODEL_NAME`、`OLLAMA_SUPPORTS_THINKING`、`KB_RERANK_MODEL`、`SEMANTIC_CACHE_*`（5）、`WIKI_COMPILE_*`（4）、`WIKI_DIAGNOSTIC_PROBES`，默认值对齐 config.py。

### 6. 新增 `scripts/check_env_consistency.py`（纯 stdlib）
- 校验 A：`.env.example` ⊇ `.env.dev` ∪ `.env.prod` 键集合
- 校验 B：解析两份 compose 的 `${VAR}`/`${VAR:-d}` 引用，对应 env 文件必须含该键（显式白名单豁免可选键）
- 任一失败非零退出 + 明确输出缺失键；纳入 CI

### 7. CI（`.github/workflows/ci.yml`）
新增 `env-consistency` 快速 job（checkout + 跑校验脚本，无依赖安装），backend-unit 依赖它。

### 8. README 部署章节同步改写
- 环境隔离策略表（--env-file + project name + 新端口表）；本地裸跑改 `APP_ENV` 说明（删除"复制 .env.dev → .env"指引）；start-prod 流程加迁移步骤说明

### 9. 分支保护（GitHub 侧，非代码）
尝试 `gh api` 设置 main 保护：required PR + required status checks（env-consistency/backend-unit/backend-integration/rag-eval/frontend）。无权限则输出手动设置指引。README 写明流程：`feature/*` → PR → CI 绿 → squash merge。

## 验证

1. `docker compose -f docker-compose.dev.yml --env-file .env.dev config` / prod 同理：无未解析 `${VAR}` 警告，backend environment 含全部键
2. `uv run python scripts/check_env_consistency.py` 通过
3. `APP_ENV=dev uv run python -c "from src.config import ENV_FILE; print(ENV_FILE)"` 输出 `.env.dev`
4. 实测并存隔离：dev 全栈 up（保持运行）→ prod 全栈 up → dev 容器无重建（`docker ps` 两套共存）、两套 backend health 均通过、postgres-dev 与 postgres-prod 同时在线
5. prod 环境 `docker compose -f docker-compose.yml exec backend alembic current` 连库正常
6. 推送 feature 分支 → Actions 全绿 → PR 合并

## 明确不做（P2 范围）
- git tag / CHANGELOG / 镜像 GIT_SHA 追溯
- 独立 staging 环境
- 根目录空 `envs/` 目录的清理（仅提及，不动）
