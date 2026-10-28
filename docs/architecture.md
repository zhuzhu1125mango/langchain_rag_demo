# 架构总览

> 定位：一页讲清系统拓扑、问答链路与数据流。细节实现以代码为准。
> 更新时间：2026-10-26

## 1. 技术栈

| 层 | 技术 |
|---|---|
| 前端 | Vue 3 + Pinia + Vue Query + Element Plus + Vite（pnpm） |
| 后端 | FastAPI + LangChain（全链路 async，Python 依赖用 uv 管理） |
| 向量库 | Milvus（standalone，依赖 etcd + MinIO） |
| 对象存储 | MinIO（文档原文件） |
| 关系库 | PostgreSQL（会话/知识库元数据/反馈/实验等） |
| 缓存 | Redis |
| 联网搜索 | Tavily（在线 API，默认 provider） |
| 模型 | Provider/Model/Route registry（`ModelManager` 统一入口）：Ollama 原生 + OpenAI-Compatible，按任务路由、能力校验与 fallback。配置优先级 **DB（P2 管理 API/设置页）> `MODEL_REGISTRY_FILE` JSON > `OLLAMA_*` legacy 自动生成**；聊天模型可热切换，Embedding 变更必须重建 Milvus 索引。开发栈当前取值：`qwen3:4b`（主模型，深度思考专用）、`qwen2.5:3b-instruct`（快模型：关闭深度思考时的直答 + 意图路由/标题/改写等辅助任务）、`bge-m3`（embedding，1024 维）、`bge-reranker-base`（重排，经 sentence-transformers 或 Ollama）。详见 [model-provider-registry-2026-10.md](design/model-provider-registry-2026-10.md) |
| 监控 | Prometheus + Grafana + Alertmanager + postgres-exporter |

## 2. 服务拓扑（docker-compose）

```
                        ┌──────────── 监控栈 ────────────┐
 用户 ──► frontend ──► backend ──┤ prometheus / grafana /         │
                        │        │ alertmanager / postgres-exporter│
                        │        └────────────────────────────────┘
                        ├──► PostgreSQL（元数据/会话）
                        ├──► Redis（缓存）
                        ├──► Milvus ◄── etcd（元数据）
                        │     └────── MinIO（Milvus 存储 + 文档原文件）
                        ├──► Tavily（联网搜索）
                        ├──► Ollama（本机部署，非容器内）
                        └──►（可选）OpenAI-Compatible API（管理员显式启用；默认关闭）
```

- 开发环境：`docker-compose.dev.yml`（前端走 Vite dev server）
- 生产环境：`docker-compose.yml`（前端 nginx，后端 FastAPI）
- 两栈端口全域错开（dev backend 8000 / prod 8001 等），`name: rag-dev` / `rag-prod` 隔离容器与卷
- 两栈基础设施端口均仅绑定 `127.0.0.1`（对外只暴露 dev frontend 5173 / prod frontend 8080——容器内非 root 监听 8080，因宿主 80 被外部服务占用而改绑宿主 8080）；MinIO 镜像使用社区 fork `pgsty/minio`（官方 `minio/minio` 镜像已下架）
- 后端启动约束（`src/main.py` 的 lifespan，按顺序）：
  1. 生产模式强制校验 `SECRET_KEY` 强度与关键凭据非空，缺失即拒绝启动
  2. 生产模式校验注册策略：`AUTH_ALLOW_REGISTRATION=true` 且未显式 `ALLOW_OPEN_REGISTRATION_IN_PROD=true` → 拒绝启动（SEC-2）
  3. 校验模型配置：DB 有 Provider 配置时按 registry 校验；否则按 `MODEL_REGISTRY_FILE` / `backend/data/model_registry.json` 校验；两者都没有时回退 legacy，强制校验 `OLLAMA_*` 三个模型名非空且存在于本地 Ollama（`/api/tags`），缺失即拒绝启动
  4. 建表（`init_db`）+ 预热 `CacheService`（Redis 为必需依赖，8 秒超时）
  5. LLM 预热为 best-effort，失败仅告警不中断启动

## 3. 问答核心链路（RAG 决策管线）

```
用户提问
  │
  ├─ 敏感词校验 / 权限校验（auth.py：API Key（全局单租户 / 用户级）与 JWT 双模式 + owner 归属校验）
  ├─ 意图路由（intent_router/：embedding 分类器 + LLM 路由 + 置信度门控）
  │    ├─ 知识库问答 ──► 策略管理（strategies/：semantic / keyword / llm_inference）
  │    ├─ 时效性问题 ──► 联网搜索（web_search_service）
  │    ├─ Agent 类问题 ──► 有界 Agent 循环（agent_orchestrator.py，AGENT_ORCHESTRATOR_ENABLED 默认开启；
  │    │    search_agent.py 的原生 FC 为回退路径）——DECIDE-ACT-OBSERVE，max_steps/时间预算熔断，
  │    │    工具经统一 ToolManager 暴露（web_search/kb_search/wiki_lookup 等），可与知识库检索混合
  │    ├─ 工具类问题 ──► 工具执行（tools/plugins：天气/时间/金价/汇率/计算器/网页抓取）
  │    └─ 闲聊/纯 LLM ──► 直接生成（answer_generator_no_context）
  │
  ├─ 查询改写（query_rewriter.py）
  ├─ 检索（hybrid_search.py：BM25 + 向量，失败回退 dense）
  ├─ 重排序（ollama_reranker.py）
  ├─ 上下文构建（context_builder.py / context_enhancer.py）
  ├─ 答案生成（answer_generator.py，ainvoke）
  └─ 后处理：引用补全（citation_backfiller）→ 数字幻觉校验（numerical_validator）
            → 输出清洗（output_sanitizer：过滤推理前缀/复述）
```

### 模型路由层（Provider / Model / Route）

所有 LLM/Embedding 调用经 `ModelManager` 从 registry 解析，不在业务层直接构造 `ChatOllama`：

| 任务 | 用途 | 能力要求 |
|---|---|---|
| `answer` | 深度思考开启时的主回答 | chat + stream（可选 reasoning） |
| `answer_direct` | 深度思考关闭时的直答 | chat + stream |
| `fast` | 通用轻量任务兜底 | chat |
| `intent_router` / `title_generation` / `query_rewrite` / `verifier` | 意图、标题、改写、校验 | chat |
| `agent_decision` | Agent 工具决策 | chat + tools |
| `wiki_compile` / `doc_analyze` | Wiki 编译、文档分析 | chat |
| `embedding` | 检索/语义缓存/意图分类/引用补全 | embedding |

- `answer` / `fast` / `embedding` 为必需路由；其他聊天任务未单独配置时回退 `fast` 路由。
- 主模型失败按 route `fallbacks` 降级；本地主模型降级到外部 Provider 需显式 `allow_external_fallback`（默认禁止，避免数据出域）。
- `agent_decision` 只接受声明 `tools` 的模型；Embedding 维度必须与 `EMBEDDING_DIMENSION` 一致。
- 配置代次（`registry_generation`）变化时，`RAGChain` / `TitleGenerator` / `WikiCompiler` 的模型缓存自动失效；在途请求继续使用旧实例。

### 熔断、用量与成本（P2.1）

- 每个 Provider 连续失败 3 次后打开 60s 熔断，期间该 Provider 的所有任务直接跳过并走 fallback；冷却后半开，只放行一次探测，成功关闭、失败重新计时。
- 熔断状态仅存进程内存；应用新 registry 后重置，避免旧 Provider 故障拖住新配置。
- 每次聊天调用按 `provider_id + model_id + task` 记录调用数、失败数、input/output tokens、平均延迟与估算成本；成本 = tokens / 1000 × 模型配置的每 1K tokens 单价，未配置价格的调用记 0 并计入 `unpriced_calls`。
- 用量增量每 `MODEL_USAGE_FLUSH_INTERVAL_S`（默认 60s）写入 `model_usage_daily`（日期 + Provider + 模型 + 任务 + 币种），应用关闭时最后 flush；写库失败回填内存，不影响请求链路。
- 指标：`rag_model_provider_calls_total`、`rag_model_tokens_total`、`rag_model_circuit_state`（1=open / 0.5=half_open / 0=closed）；管理接口 `GET /api/admin/models/usage?days=7` 返回持久化聚合 + 未落库增量。
- 设置页「模型服务」已展示用量/成本并支持单价编辑；成本上限/429 告警仍为待办；Embedding 调用暂未纳入熔断/用量。

### Embedding 迁移与 Reranker（P3）

- Embedding 模型/维度以 registry 为权威；`model_index_profile` 记录 collection 当前画像与状态。保存新 Embedding 配置后，apply 会因画像不一致返回 409，必须先重建索引。
- 重建：读取线上集合标量字段 → 用目标 Embedding 重算 dense + 复用 BM25 词表重算 sparse → 写入 `{collection}__rebuild_*` → 原子 rename；失败保留线上，成功后旧集合改名 `{collection}__backup_*` 供回滚。
- 重建/回滚会全量失效语义缓存，并按 registry 代次刷新意图分类、Wiki 编译、联网搜索与 MilvusService 的 Embedding 实例。
- 可选 `rerank` 路由（Ollama 托管）优先于 `KB_RERANK_*` env；registry 变更时 `KBReranker` 热重建。Sentence-Transformers 本地 reranker 仍走 env。

### 流式响应（SSE）

- 路由：`POST /api/chat/stream`，`StreamingResponse` 推送事件
- 服务端**只发 `data:` 行，不使用 SSE 的 `event:` 字段**，事件类型由 JSON 内的 `type` 表达
- 事件类型（6 种，无独立的 `sources` 事件，来源随 `end` 下发）：`reasoning`（分步：意图路由/检索/搜索/工具/思考，含 step/status/title/content/metadata）、`thinking`（模型原始思考增量）、`content`（回答片段）、`end`（含 message_id/session_id/sources/answer_type/reasoning）、`title`（在 `end` **之后**补发）、`error`（经脱敏，含 request_id）
- 前端将 reasoning 事件渲染为答案气泡上方的可折叠时间线；详见 [api.md §1.2](api.md)

## 4. 数据与存储职责

| 存储 | 内容 |
|---|---|
| PostgreSQL | 用户（`users`）、知识库/文档元数据、会话与消息（JSONB）、反馈/坏例、实验（A/B）、标签/分类、请求 trace、Wiki 编译页（`wiki_pages`）、审计日志（`audit_logs`）、学习引擎配置、模型注册（`model_providers` / `model_deployments` / `model_routes`）、模型用量日聚合（`model_usage_daily`）、索引 Embedding 画像（`model_index_profile`） |
| Milvus | 文档向量 + BM25 稀疏向量（含词表持久化）；Wiki 编译页切片以 `source_kind="wiki"` 同集合区分；Agent 记忆以 `source_kind="memory"` 复用同集合 |
| MinIO | 文档原文件、Milvus 底层存储 |
| Redis | 缓存（知识库列表、快捷问题等）；语义缓存条目；Wiki 分布式锁（`WIKI_DISTRIBUTED_LOCK`，Redis 不可用时降级为进程内锁）。注：Wiki 编译**去抖队列是进程内实现**（`wiki_compile_scheduler.py` 用 per-KB 集合 + asyncio 计时器，多副本部署时各 worker 独立去抖） |

## 5. 后端代码地图（backend/src/）

| 目录/模块 | 职责 |
|---|---|
| `api/` | 路由层（18 个模块，不写业务逻辑）：auth、chat、knowledge_base、wiki、document、session、trace、audit、category、tag、feedback、badcase、learning、experiment、config、evaluation、notification、admin_models。`document` 已拆为包（CODE-1）：`common`（模型/归属校验/序列化，无路由）、`upload`、`query`、`manage`，装配顺序 upload→query→manage（静态路径先于 `/{doc_id}`） |
| `services/document_tasks.py` | 文档后台任务与索引一致性（自 `api/document.py` 抽出）：处理/删除任务、SEC-4 上下架向量一致性、Wiki 编译阶段、文本相似度 |
| `services/` | 业务核心：决策管线、检索、生成、搜索、工具、评估、学习引擎、模型注册/健康/用量 |
| `services/model_registry.py` | Provider/Model/Route Pydantic 配置、legacy Ollama bootstrap、JSON 加载、能力/路由/维度校验、跨 Provider fallback 过滤 |
| `services/model_adapters.py` | Ollama / OpenAI-Compatible 的 Chat 与 Embedding 适配器；`ChatModelGroup` 按序 fallback（跳过熔断 Provider）、记录用量并保留 `bind_tools` |
| `services/model_manager.py` | 模型统一入口：按任务路由、registry 热加载/DB 应用、配置代次、Embedding 实例缓存 |
| `services/model_health.py` | Provider 级熔断：连续失败阈值、冷却半开探测、跳过热熔断 Provider；registry 变更时重置 |
| `services/model_usage.py` | 进程内用量聚合：Provider/模型/任务的调用、失败、Token、平均延迟、成本估算与未落库增量 |
| `services/model_usage_store.py` | 用量增量周期落库、最近 N 天聚合查询、持久化与进程内数据合并 |
| `services/embedding_migration.py` | Embedding 索引画像、apply 安全闸门、影子集合重建/原子切换/回滚、缓存失效编排 |
| `services/model_config_service.py` | P2 DB↔registry 转换、Provider/模型/路由 CRUD、连接测试/模型发现、校验并应用 |
| `services/model_secret_cipher.py` | Provider API Key Fernet 加解密（`MODEL_SECRET_ENC_KEY`，缺省从 `SECRET_KEY` 派生） |
| `services/account_service.py` | 账号数据导出与注销级联清理（SEC-14） |
| `services/rag_chain.py` | RAG 问答链编排门面：依赖装配（`__init__`/`_async_init`）+ 公开 API（`run`/`arun_stream`）+ 生命周期；管线各阶段已拆入 `services/pipeline/`（文件已从约 2000 行瘦身为门面） |
| `services/pipeline/` | 管线拆分（自 `rag_chain.py` 抽出）：`state.py`（共享状态 / ContextVar / 提示词模板）、`stages.py`（阶段 1-9 编排 + 终态清理）、`retrieval.py`、`agent.py`、`generation.py`、`insights.py` |
| `services/agent_orchestrator.py` | 有界 Agent 编排：plan-execute 循环（max_steps=3 + 时间预算熔断），KB/Wiki/搜索工具统一接入，`AGENT_ORCHESTRATOR_ENABLED` 默认开启（W6 灰度转正 2026-09-29；有界循环自带降级链）；SEC-5：不可信观察隔离、工具出站域名/DLP、`fetch_webpage` 默认关闭 |
| `services/guardrails/` | AM-2 确定性护栏：input/output/tool/approval + SEC-5 `context_guard`（不可信上下文包裹、注入信号检测）与流式输出跨 chunk 脱敏 |
| `services/context_enhancer.py` / `context_builder.py` | 历史摘要增强 / 上下文预算与 Lost-in-the-Middle 重排 |
| `services/semantic_cache_service.py` | 语义缓存（P1-3）：命中直接回放，Redis 不可用时 fail-open |
| `services/wiki_*.py` | LLM-Wiki 编译层：编译器、去抖调度、级联重写、交叉链接扩展、一致性 lint、分布式锁 |
| `services/ocr_parser.py` | 扫描版 PDF OCR 深度解析：扫描版检测（字符密度）+ 双后端（MinerU pipeline / PaddleOCR PP-StructureV3，可选依赖）分流，输出 Markdown 复用结构化分块，失败回退内置解析 |
| `services/document_loaders.py` | 本地直读文档加载器（txt/md/csv/json/pdf，PDF 基于 pypdf）；Office/HTML/EPUB 暂走 langchain-community unstructured（待迁） |
| `services/intent_router/` | 意图识别：embedding 分类器、LLM 路由、置信度门控、工具注册表 |
| `services/strategies/` | 检索策略：semantic / keyword / llm_inference + 策略管理器 |
| `services/tools/plugins/` | 工具插件（9 个）：web_search、fetch_webpage、weather、datetime、gold_price、exchange_rate、calculator、kb_search、wiki_lookup |
| `services/evaluation/` | 检索评估器 + 生成评估器（faithfulness/relevance） |
| `services/trace_collector.py` | 请求链路追踪采集（对应 `/api/traces`） |
| `services/audit_service.py` | 审计日志记录与查询（P2-4，best-effort 写入不阻断主流程，对应 `/api/audit`） |
| `middleware/` | Prometheus 指标、自定义 Metrics 中间件 |
| `utils/` | AsyncSingleton（服务单例统一基类）、安全工具、敏感词、校验器（含 `validate_kb_ownership` 归属校验）、`rate_limit.py`（受信代理 IP + Redis/进程内限流）、`quota.py`（聊天/上传/评估配额）、`safe_http.py`（连接层固定 IP 的 SSRF 安全客户端）、`log_context.py`（request_id/user/stage 结构化日志上下文） |
| `models/strategy.py` | 学习样本：`strategy_executions.user_id`（SEC-1 owner 隔离） |
| `auth.py` | 认证：`X-API-Key`（全局单租户 / 用户级 API Key，后者解析为真实 `user_id`）+ JWT 多用户（注册/登录/`/me`/`/refresh`）+ `require_owner` 对象级授权 + WS 首帧鉴权 |
| `prompts/` | 提示词模板（aiofiles 异步加载） |

## 6. 关键设计约定

- **全异步**：IO 操作（LLM 调用、文件读写、MinIO）必须 async；CPU 密集（PDF 解析、embedding 编码）用 `asyncio.to_thread` 下线程池（注意：`web_search_service` 的模型加载与网页解析仍是同步调用，属已知缺陷，见 `docs/archive/code-review-2026-09.md` P1-B3/B4）
- **单例统一**：服务单例继承 `AsyncSingleton`，统一异步初始化/清理生命周期
- **可降级**：混合检索失败回退 dense；联网搜索失败不影响知识库回答；工具失败降级为 LLM 直答
- **输出净化**：答案不携带推理前缀、不整段复述原文，关键事实标注 `[n]` 来源编号
- **安全边界（SEC-1~5）**：主资源按 `owner_id` 隔离；学习样本 owner 过滤；生产默认关闭开放注册；受信代理 IP + Redis 限流；聊天/上传配额；下架/移动 KB 与向量、语义缓存保持一致；检索/网页/工具观察按不可信上下文隔离，工具出站受域名/DLP 约束，`fetch_webpage` 默认关闭
- **安全边界（SEC-6~21）**：实验/通知/分类标签归属收口；JWT `jti` 吊销 + 登出；SSRF 连接层固定 IP（`safe_http`）+ 工具 URL 参数 DNS fail-closed；`/health/live`/`ready` 拆分；账号导出/注销与日志保留期清扫；评估 API 用户级配额；会话分页与批量上限；前端 CSP `connect-src 'self'` + 条件 HSTS（详见 [security-multitenancy-hardening-2026-10.md](design/security-multitenancy-hardening-2026-10.md)）
- **离线优先**：默认所有模型本地部署（Ollama），无需付费 API；外部 OpenAI-Compatible Provider 为**可选能力**，需管理员在「设置 → 模型服务」显式添加并启用，本地主模型降级到外部默认禁止（`allow_external_fallback=false`），避免数据出域

## 7. 相关文档

- API 细节：[api.md](api.md)
- 部署：[guide/deployment.md](guide/deployment.md)
- 开发与测试：[guide/development.md](guide/development.md)
- 监控运维：[guide/monitoring.md](guide/monitoring.md)
- 后续优化：[design/improvement-roadmap.md](design/improvement-roadmap.md)
- 模型 Provider/路由：[design/model-provider-registry-2026-10.md](design/model-provider-registry-2026-10.md)
- 安全与多租户：[design/security-multitenancy-hardening-2026-10.md](design/security-multitenancy-hardening-2026-10.md)
- 专项设计：见 `design/` 目录
