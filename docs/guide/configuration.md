# 配置说明

> 从 README 抽出（2026-11-07 结项整理）。README 只留定位与上手，配置细节收在这里。
> 关联：[deployment.md](deployment.md)（环境分离与部署步骤）、[development.md](development.md)（本地开发与测试）。

## 1. 环境变量配置

项目包含多个环境变量配置文件，用于不同层次的配置管理。

### 1.1 配置文件

| 文件 | 用途 | 使用场景 |
|------|------|---------|
| `.env.dev` | 开发环境全量业务配置 | 本地裸跑（默认）与 dev 容器（`env_file:` 注入）共用 |
| `.env.prod` | 生产环境全量业务配置 | 生产栈经 `--env-file` + `env_file:` 注入容器 |
| `.env.example` | 全量配置模板（含注释） | 创建 `.env.dev` / `.env.prod` 时对照；CI 校验其覆盖全部在用键 |

### 1.2 加载机制

**后端配置加载逻辑**（`backend/src/config.py`）：

- 由 `APP_ENV` 环境变量选择环境文件：`dev`（默认）→ `.env.dev`，`prod` → `.env.prod`
- Docker 部署时，业务配置统一由 compose `env_file:` 注入为容器环境变量，优先级高于 env 文件；
  compose 文件仅保留 Docker 网络路由覆盖（如 `POSTGRES_HOST=postgres`），新增功能开关只改 env 文件，不再改 compose
- `IS_PRODUCTION` 决定启动强校验（强密钥/凭据非空）：容器内由 `IN_DOCKER=true` 推断为生产；
  非 Docker 的生产部署（裸跑 uvicorn/systemd）必须显式设置 `APP_ENV=prod`（兼容旧值 `production`），否则不执行强校验

> ⚠️ 写 env 文件时注意两件事（`scripts/check_env_consistency.py` 的校验 D / E 会拦，CI 里跑）：
>
> 1. **空值后面不能跟行内注释**（`KEY=    # 说明`）。compose 的 `env_file` 解析**不**剥行内注释，
>    会把 `# 说明` 当值注入容器，pydantic 抛 `int_parsing`，后端启动即退出——
>    这时候 config 默认值和 field_validator 都救不了，值压根到不了校验层。
>    注释要挪到上一行，本行只留 `KEY=`。
>    （**非空值**带行内注释是允许的：本模板里大量这么写，如 `CHUNK_SIZE=512  # 注释`。）
> 2. **被 compose `${VAR}` 插值的键不能带行内注释**。两套解析器行为不同：
>    compose `--env-file` 插值**不**剥注释、容器 `env_file:` 注入**会**剥，
>    于是同一个变量两端取到不同的值（如 `dev_minio    # 管理员账号` vs `dev_minio`），
>    表现为 MinIO 凭据不匹配、`/health/ready` 直接 503，而容器状态还是 healthy。

### 1.3 环境差异对照

| 配置项 | 开发环境 (`.env.dev`) | 生产环境 (`.env.prod`) |
|--------|---------------------|----------------------|
| Compose Project | `rag-dev` | `rag-prod` |
| 数据库名 | `app_dev` | `app_prod` |
| 用户名 | `dev_user` | `prod_user` |
| 密码 | 简单密码 | **必须手动设置强密码** |
| 端口占用 | 常规端口（8000 / 5173 / 5433…） | 错开端口（80 / 8001 / 5434…，仅回环绑定） |
| MinIO 安全 | `false` | `true` |
| 重启策略 | `no` | `unless-stopped` |
| 数据卷 | `*_dev` | `*_prod` |
| 监控告警 | 不启用 Alertmanager | 启用 Alertmanager |
| 后端运行方式 | 源码挂载 + 热重载 | 镜像构建 |
| 前端运行方式 | Vite 开发服务器 | Nginx 静态服务 |

## 2. 配置示例

```env
# ===========================================
# 开发环境配置 (.env.dev)
# ===========================================
POSTGRES_DB=app_dev
POSTGRES_USER=dev_user
POSTGRES_PASSWORD=dev_password
MINIO_ROOT_USER=dev_minio
MINIO_ROOT_PASSWORD=dev_minio_password
MINIO_ACCESS_KEY=dev_access_key
MINIO_SECRET_KEY=dev_secret_key
MINIO_BUCKET_NAME=documents
MINIO_SECURE=false

# Redis
REDIS_PASSWORD=dev_redis_password

# 安全密钥：开发环境可不设置，后端启动时自动生成临时随机密钥
# （重启后旧签名失效）；生产环境必须设置为强密钥
SECRET_KEY=
```

```env
# ===========================================
# 生产环境配置 (.env.prod)
# 注意：注释一律单独成行，不要写在 KEY= 后面（见 §1.2 的两个坑）
# ===========================================

# 以下全部必须手动设置为强密码/强密钥，否则后端启动失败
POSTGRES_DB=app_prod
POSTGRES_USER=prod_user
POSTGRES_PASSWORD=
MINIO_ROOT_USER=prod_minio
MINIO_ROOT_PASSWORD=
MINIO_ACCESS_KEY=
MINIO_SECRET_KEY=
REDIS_PASSWORD=
SECRET_KEY=

MINIO_BUCKET_NAME=documents
MINIO_SECURE=true

# 非 Docker 的生产部署（直接 uvicorn/systemd 运行）必须设置 APP_ENV=prod，
# 否则不执行强校验（兼容旧值 "production"）；Docker 部署无需设置，由 IN_DOCKER 推断
APP_ENV=prod

# Milvus 集合 schema/维度不匹配时是否允许删除重建（破坏性！旧向量数据全部丢失）
# 默认 false：不匹配时启动失败并提示；确认可接受数据丢失后才改为 true
MILVUS_REBUILD_ON_MISMATCH=false
```

> **`MINIO_ACCESS_KEY` 必须与 `MINIO_ROOT_USER` 一致**（这里两个都叫 `prod_minio`）。
> 两者不一致时后端连 MinIO 会 503，且日志只写「连接失败」，看起来像 MinIO 没起来——
> 2026-11-04 部署时踩过，对照实验才定位到。

## 3. 核心配置参数

> **「默认值」一列的口径**：有代码默认值的项填 `backend/src/config.py` 里的默认值；
> 必须由 env 提供、代码里为 `None`/不设默认的项（模型名、凭据、Provider 选择）
> 填 `.env.example` 里的**开发栈取值**——这类项写代码默认值只会误导。
> 两者不一致时在「说明」列注明。

| 参数 | 说明 | 默认值 | 范围 |
|------|------|--------|------|
| `CHUNK_SIZE` | 文本分割大小（字符数） | 512 | 50-5000 |
| `CHUNK_OVERLAP` | 分割重叠大小 | 64 | ≥0 且 < `CHUNK_SIZE` |
| `TOP_K` | 检索文档数量 | 3 | 1-20 |
| `POSTGRES_PORT` | 数据库端口 | 5433（本地）/ 5432（容器内） | - |
| `MINIO_ROOT_USER` | MinIO 管理员账号 | - | - |
| `MINIO_ROOT_PASSWORD` | MinIO 管理员密码 | - | - |
| `MINIO_ACCESS_KEY` | 后端连接 MinIO 的 Access Key | - | 须与 `MINIO_ROOT_USER` 一致 |
| `MINIO_SECRET_KEY` | 后端连接 MinIO 的 Secret Key | - | 须与 `MINIO_ROOT_PASSWORD` 一致 |
| `MINIO_BUCKET_NAME` | MinIO 存储桶名称 | `documents` | - |
| `MINIO_SECURE` | 是否启用 HTTPS | `false` | true/false；**代码默认 `true`**（生产模式），内网 compose 显式设 `false` |
| `SECRET_KEY` | JWT/安全签名密钥 | 无（开发缺失时启动自动生成临时密钥） | 生产环境必须设置为强密钥，否则启动失败 |
| `AUTH_ALLOW_REGISTRATION` | 是否开放注册 | `true`（开发） | 生产建议 `false`；生产为 true 且未显式确认时拒绝启动 |
| `ALLOW_OPEN_REGISTRATION_IN_PROD` | 生产显式允许开放注册 | `false` | 仅确需开放注册时设 true |
| `TRUSTED_PROXY_CIDRS` | 受信代理网段（逗号分隔 CIDR/IP） | `127.0.0.1/32,::1/128` | Docker 由 compose 注入内网网段；仅受信代理的 XFF/X-Real-IP 生效 |
| `QUOTA_ENFORCEMENT_ENABLED` | 聊天/上传配额总开关 | `true` | true/false；0 配额表示不限 |
| `CHAT_RATE_LIMIT_PER_MINUTE` | 聊天每分钟请求上限 | `10` | - |
| `CHAT_MAX_CONCURRENT_STREAMS_PER_USER` | 每用户并发流上限 | `2` | - |
| `CHAT_DAILY_MESSAGE_QUOTA` | 每用户每日消息上限 | `200` | - |
| `UPLOAD_MAX_FILES_PER_BATCH` | 单次批量上传文件数上限 | `20` | - |
| `UPLOAD_MAX_BATCH_MB` | 单批上传总大小上限（MB） | `200` | - |
| `UPLOAD_DAILY_MB_PER_USER` | 每用户每日上传字节上限（MB） | `500` | - |
| `USER_API_KEYS_ENABLED` | 用户级 API Key 开关（设置页自助签发/吊销） | `false` | true/false；开启后用户可签发 `rag_` 前缀 Key，以其本人 `user_id` 调 API（owner 隔离同 JWT），MCP/脚本/CI 集成用 |
| `USER_API_KEYS_MAX_PER_USER` | 每用户 Key 数量上限 | `5` | - |
| `AUTH_RATE_LIMIT_API_KEY_CREATE` | 签发端点限流（次/分/IP） | `5` | 0 = 不限流 |
| `APP_ENV` | 显式环境标记 | 空（按 `IN_DOCKER` 推断） | `dev` / `prod`（兼容旧值 `production`）。非 Docker 的生产部署**必须显式设为 `prod`**，否则不执行启动强校验 |
| `MILVUS_REBUILD_ON_MISMATCH` | 集合 schema/维度不匹配时是否删除重建 | `false` | 破坏性操作，开启前必须完成数据迁移/备份 |
| `REDIS_HOST` | Redis 主机 | `localhost` | 容器内自动覆盖为 `redis` |
| `REDIS_PORT` | Redis 端口 | `6379` | - |
| `REDIS_DB` | Redis 数据库编号 | `0` | 0-15 |
| `REDIS_PASSWORD` | Redis 密码 | `dev_redis_password` | 必填，生产环境必须设置为强密码 |
| `SEARCH_PROVIDER` | 搜索引擎 | `tavily` | `duckduckgo` / `searxng` / `tavily` |
| `SEARCH_API_KEY` | Tavily API Key（provider 为 tavily 时必填） | - | - |
| `SEARCH_MAX_RESULTS` | 单次搜索返回结果数 | 10 | 1-20 |
| `SEARCH_FETCH_TIMEOUT` | 网页抓取超时（秒） | 10 | - |
| `SEARCH_MAX_FETCH` | 最大并发抓取网页数 | 5 | - |
| `SEARCH_MIN_CONTENT_LENGTH` | 网页内容最小长度 | 100 | - |
| `SEARCH_ENABLE_MULTI_QUERY` | 是否启用 LLM 多角度 Query 改写 | `true` | true/false |
| `SEARCH_NUM_QUERIES` | Query 改写生成的查询数 | 3 | - |
| `SEARCH_ENABLE_RERANK` | 是否启用 Cross-Encoder 语义重排 | `true` | true/false |
| `EMBEDDING_DIMENSION` | Embedding 向量维度 | `1024` | 需与 `EMBEDDING_MODEL_NAME` 对应 |
| `FAST_LLM_MODEL_NAME` | 轻量任务模型 | `qwen2.5:3b-instruct` | 意图路由/标题/Query 改写；须与 `OLLAMA_DIRECT_MODEL_NAME` 一致 |
| `KB_RERANK_MODEL` | 知识库重排模型 | `BAAI/bge-reranker-base` | sentence_transformers 从 HF_HOME 离线缓存加载 |
| `KB_RERANK_PROVIDER` | 知识库重排加载方式 | `sentence_transformers` | `sentence_transformers` / `ollama`；**代码默认 `ollama`**，开发栈用前者（本地离线加载，免走 Ollama） |
| `SEARCH_RERANK_MODEL` | 搜索重排模型 | `BAAI/bge-reranker-base` | 同上 |
| `SEARCH_RERANK_PROVIDER` | 搜索重排加载方式 | `sentence_transformers` | `sentence_transformers` / `ollama`；**代码默认 `ollama`**，开发栈用前者 |
| `SEARCH_RERANK_TOP_K` | 重排后返回 Top-K | 5 | - |
| `SEARCH_CACHE_TTL` | 搜索结果缓存时间（秒） | 3600 | - |
| `SEARCH_CONTENT_CACHE_TTL` | 网页内容缓存时间（秒） | 86400 | - |
| `SEARCH_ENABLE_FUNCTION_CALLING` | 是否启用 Function Calling | `true` | true/false |
| `AGENT_WEB_FETCH_ENABLED` | 是否向 Agent 暴露网页深度抓取（`fetch_webpage`） | `false` | SEC-5：默认关闭；开启后受审批/域名/DLP 策略约束 |
| `GUARDRAIL_TOOL_DOMAIN_ALLOWLIST` / `GUARDRAIL_TOOL_DOMAIN_DENYLIST` | 工具出站域名白/黑名单（逗号分隔） | 空 | 空 allowlist = 允许任意公网域名；denylist 优先 |
| `AGENT_APPROVAL_MODE` / `AGENT_APPROVAL_TOOLS` | HITL 审批模式 / 强制审批工具 | `off` / `fetch_webpage` | 审批模式 off 时名单不生效；开启 wait/reject 后生效 |
| `SEARCH_ENABLE_REACT` | 是否启用 ReAct Agent | `false` | true/false |
| `SEARCH_REACT_MAX_STEPS` | ReAct 最大推理步数 | 3 | - |
| `SEARCH_AGENT_FALLBACK_TO_PHASE2` | Agent 失败是否回退到普通搜索 | `true` | true/false |
| `TITLE_GENERATION_ENABLED` | 是否启用 LLM 自动生成会话标题 | `true` | true/false |
| `TITLE_MAX_LENGTH` | 生成标题的最大长度 | `30` | - |
| `TITLE_FALLBACK_LENGTH` | 生成失败时截取问题前 N 字 | `30` | - |
| `TITLE_GENERATION_MODEL` | 指定标题生成模型 | - | 留空则使用 `FAST_LLM_MODEL_NAME` |
| `MODEL_REGISTRY_FILE` | 可选：JSON registry 路径 | - | 相对路径以 `backend/` 为根；配置后优先于 legacy `OLLAMA_*` |
| `MODEL_USAGE_FLUSH_INTERVAL_S` | 可选：用量增量落库间隔（秒） | `60` | `0` 关闭周期落库，查询仍叠加进程内增量 |
| `MODEL_SECRET_ENC_KEY` | Provider API Key 加密密钥 | - | 未设置时从 `SECRET_KEY` 派生；轮换后已存 Key 需重录 |

> 模型相关 `OLLAMA_*` / `FAST_LLM_MODEL_NAME` / `TITLE_GENERATION_MODEL` 仅在 legacy 模式下生效；
> DB 或 `MODEL_REGISTRY_FILE` 存在时以 registry 的任务路由为准。

## 4. 服务端口

| 服务 | 开发环境 | 生产环境 | 说明 |
|------|---------|---------|------|
| 后端 API | 8000 | 127.0.0.1:8001 | FastAPI 服务 |
| 前端 | 5173 | 80 | Vite（开发）/ Nginx（生产） |
| PostgreSQL | 5433 | 127.0.0.1:5434 | 业务数据库 |
| MinIO | 9000 | 127.0.0.1:9002 | 对象存储（S3 API） |
| MinIO 控制台 | 9001 | 127.0.0.1:9003 | 管理界面 |
| Milvus | 19530 | 127.0.0.1:19531 | 向量数据库 |
| Milvus 健康检查 | 9091 | 127.0.0.1:9092 | 健康检查/指标端口 |
| Redis | 6379 | 127.0.0.1:6380 | 缓存服务 |
| Prometheus | 9090 | 127.0.0.1:9094 | 监控指标 |
| Alertmanager | -（开发不部署） | 127.0.0.1:9095 | 告警管理 |
| Grafana | 3000 | 127.0.0.1:3001 | 监控面板 |

**注意：** 两套环境端口完全错开，可**同时运行**（双栈并存），数据完全隔离；
生产栈基础设施端口仅绑定回环地址（127.0.0.1），不对局域网暴露。
