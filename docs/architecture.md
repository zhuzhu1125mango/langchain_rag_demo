# 架构总览

> 定位：一页讲清系统拓扑、问答链路与数据流。细节实现以代码为准。
> 更新时间：2026-10-03

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
| 模型 | Ollama 本地模型，**模型名一律由 `.env` 提供、代码无内置默认**（A2 去硬编码），启动时经 `/api/tags` 校验存在性。开发栈当前取值：`qwen3:4b`（主模型，深度思考专用）、`qwen2.5:3b-instruct`（快模型：关闭深度思考时的直答 + 意图路由/标题/改写等辅助任务）、`bge-m3`（embedding，1024 维）、`bge-reranker-base`（重排，经 sentence-transformers 或 Ollama） |
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
                        └──► Ollama（本机部署，非容器内）
```

- 开发环境：`docker-compose.dev.yml`（前端走 Vite dev server）
- 生产环境：`docker-compose.yml`（前端 nginx，后端 FastAPI）
- 两栈端口全域错开（dev backend 8000 / prod 8001 等），`name: rag-dev` / `rag-prod` 隔离容器与卷
- 两栈基础设施端口均仅绑定 `127.0.0.1`（对外只暴露 dev frontend 5173 / prod frontend 8080——容器内非 root 监听 8080，因宿主 80 被外部服务占用而改绑宿主 8080）；MinIO 镜像使用社区 fork `pgsty/minio`（官方 `minio/minio` 镜像已下架）
- 后端启动约束（`src/main.py` 的 lifespan，按顺序）：
  1. 生产模式强制校验 `SECRET_KEY` 强度与关键凭据非空，缺失即拒绝启动
  2. 生产模式校验注册策略：`AUTH_ALLOW_REGISTRATION=true` 且未显式 `ALLOW_OPEN_REGISTRATION_IN_PROD=true` → 拒绝启动（SEC-2）
  3. 校验三个模型名非空且存在于本地 Ollama（`/api/tags`），缺失即拒绝启动
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

### 流式响应（SSE）

- 路由：`POST /api/chat/stream`，`StreamingResponse` 推送事件
- 服务端**只发 `data:` 行，不使用 SSE 的 `event:` 字段**，事件类型由 JSON 内的 `type` 表达
- 事件类型（6 种，无独立的 `sources` 事件，来源随 `end` 下发）：`reasoning`（分步：意图路由/检索/搜索/工具/思考，含 step/status/title/content/metadata）、`thinking`（模型原始思考增量）、`content`（回答片段）、`end`（含 message_id/session_id/sources/answer_type/reasoning）、`title`（在 `end` **之后**补发）、`error`（经脱敏，含 request_id）
- 前端将 reasoning 事件渲染为答案气泡上方的可折叠时间线；详见 [api.md §1.2](api.md)

## 4. 数据与存储职责

| 存储 | 内容 |
|---|---|
| PostgreSQL | 用户（`users`）、知识库/文档元数据、会话与消息（JSONB）、反馈/坏例、实验（A/B）、标签/分类、请求 trace、Wiki 编译页（`wiki_pages`）、审计日志（`audit_logs`）、学习引擎配置 |
| Milvus | 文档向量 + BM25 稀疏向量（含词表持久化）；Wiki 编译页切片以 `source_kind="wiki"` 同集合区分；Agent 记忆以 `source_kind="memory"` 复用同集合 |
| MinIO | 文档原文件、Milvus 底层存储 |
| Redis | 缓存（知识库列表、快捷问题等）；语义缓存条目；Wiki 分布式锁（`WIKI_DISTRIBUTED_LOCK`，Redis 不可用时降级为进程内锁）。注：Wiki 编译**去抖队列是进程内实现**（`wiki_compile_scheduler.py` 用 per-KB 集合 + asyncio 计时器，多副本部署时各 worker 独立去抖） |

## 5. 后端代码地图（backend/src/）

| 目录/模块 | 职责 |
|---|---|
| `api/` | 路由层（17 个模块，不写业务逻辑）：auth、chat、knowledge_base、wiki、document、session、trace、audit、category、tag、feedback、badcase、learning、experiment、config、evaluation、notification |
| `services/` | 业务核心：决策管线、检索、生成、搜索、工具、评估、学习引擎 |
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
| `utils/` | AsyncSingleton（服务单例统一基类）、安全工具、敏感词、校验器（含 `validate_kb_ownership` 归属校验）、`rate_limit.py`（受信代理 IP + Redis/进程内限流）、`quota.py`（聊天/上传资源配额） |
| `models/strategy.py` | 学习样本：`strategy_executions.user_id`（SEC-1 owner 隔离） |
| `auth.py` | 认证：`X-API-Key`（全局单租户 / 用户级 API Key，后者解析为真实 `user_id`）+ JWT 多用户（注册/登录/`/me`/`/refresh`）+ `require_owner` 对象级授权 + WS 首帧鉴权 |
| `prompts/` | 提示词模板（aiofiles 异步加载） |

## 6. 关键设计约定

- **全异步**：IO 操作（LLM 调用、文件读写、MinIO）必须 async；CPU 密集（PDF 解析、embedding 编码）用 `asyncio.to_thread` 下线程池（注意：`web_search_service` 的模型加载与网页解析仍是同步调用，属已知缺陷，见 `docs/archive/code-review-2026-09.md` P1-B3/B4）
- **单例统一**：服务单例继承 `AsyncSingleton`，统一异步初始化/清理生命周期
- **可降级**：混合检索失败回退 dense；联网搜索失败不影响知识库回答；工具失败降级为 LLM 直答
- **输出净化**：答案不携带推理前缀、不整段复述原文，关键事实标注 `[n]` 来源编号
- **安全边界（SEC-1~5）**：主资源按 `owner_id` 隔离；学习样本 owner 过滤；生产默认关闭开放注册；受信代理 IP + Redis 限流；聊天/上传配额；下架/移动 KB 与向量、语义缓存保持一致；检索/网页/工具观察按不可信上下文隔离，工具出站受域名/DLP 约束，`fetch_webpage` 默认关闭
- **离线优先**：所有模型本地部署，禁用付费 API

## 7. 相关文档

- API 细节：[api.md](api.md)
- 部署：[guide/deployment.md](guide/deployment.md)
- 开发与测试：[guide/development.md](guide/development.md)
- 监控运维：[guide/monitoring.md](guide/monitoring.md)
- 后续优化：[design/improvement-roadmap.md](design/improvement-roadmap.md)
- 安全与多租户：[design/security-multitenancy-hardening-2026-10.md](design/security-multitenancy-hardening-2026-10.md)
- 专项设计：见 `design/` 目录
