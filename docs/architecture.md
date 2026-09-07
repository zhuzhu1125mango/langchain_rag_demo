# 架构总览

> 定位：一页讲清系统拓扑、问答链路与数据流。细节实现以代码为准。
> 更新时间：2026-09-01

## 1. 技术栈

| 层 | 技术 |
|---|---|
| 前端 | Vue 3 + Pinia + Vue Query + Element Plus + Vite（pnpm） |
| 后端 | FastAPI + LangChain（全链路 async，Python 依赖用 uv 管理） |
| 向量库 | Milvus（standalone，依赖 etcd + MinIO） |
| 对象存储 | MinIO（文档原文件） |
| 关系库 | PostgreSQL（会话/知识库元数据/反馈/实验等） |
| 缓存 | Redis |
| 联网搜索 | SearXNG（私有化，默认 provider） |
| 模型 | Ollama 本地模型：deepseek-r1:7b（推理）、qwen2.5:7b（生成）、bge-m3（embedding）、bge-reranker-v2-m3（重排） |
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
                        ├──► SearXNG（联网搜索）
                        └──► Ollama（本机部署，非容器内）
```

- 开发环境：`docker-compose.dev.yml`（前端走 Vite dev server）
- 生产环境：`docker-compose.yml`（前端 nginx，后端 FastAPI）
- 后端启动约束：MinIO/Milvus 连接失败超过 30 秒则暂停启动（fail-fast）

## 3. 问答核心链路（RAG 决策管线）

```
用户提问
  │
  ├─ 敏感词校验 / 权限校验（auth.py，API Key 模式）
  ├─ 意图路由（intent_router/：embedding 分类器 + LLM 路由 + 置信度门控）
  │    ├─ 知识库问答 ──► 策略管理（strategies/：semantic / keyword / llm_inference）
  │    ├─ 时效性问题 ──► 联网搜索（web_search_service / search_agent：Function Calling / ReAct）
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
- 事件类型：`reasoning`（分步：意图路由/检索/搜索/工具/思考，含 stage/status/title/content）、内容 chunk、`sources`、错误事件（经脱敏）
- 前端将 reasoning 事件渲染为答案气泡上方的可折叠时间线

## 4. 数据与存储职责

| 存储 | 内容 |
|---|---|
| PostgreSQL | 知识库/文档元数据、会话与消息（JSONB）、反馈/坏例、实验（A/B）、标签/分类、请求 trace、学习引擎配置 |
| Milvus | 文档向量 + BM25 稀疏向量（含词表持久化） |
| MinIO | 文档原文件、Milvus 底层存储 |
| Redis | 缓存（知识库列表/快捷问题等） |

## 5. 后端代码地图（backend/src/）

| 目录/模块 | 职责 |
|---|---|
| `api/` | 路由层：chat、knowledge_base、document、session、learning、experiment、feedback、badcase、notification 等（不写业务逻辑） |
| `services/` | 业务核心：决策管线、检索、生成、搜索、工具、评估、学习引擎 |
| `services/ocr_parser.py` | 扫描版 PDF OCR 深度解析：扫描版检测（字符密度）+ 双后端（MinerU pipeline / PaddleOCR PP-StructureV3，可选依赖）分流，输出 Markdown 复用结构化分块，失败回退内置解析 |
| `services/intent_router/` | 意图识别：embedding 分类器、LLM 路由、置信度门控、工具注册表 |
| `services/strategies/` | 检索策略：semantic / keyword / llm_inference + 策略管理器 |
| `services/tools/plugins/` | 工具插件：web_search、weather、datetime、gold_price、exchange_rate、calculator、fetch_webpage |
| `services/evaluation/` | 检索评估器 + 生成评估器（faithfulness/relevance） |
| `middleware/` | Prometheus 指标 |
| `utils/` | AsyncSingleton（服务单例统一基类）、安全工具、敏感词、校验器 |
| `auth.py` | 认证：API Key（当前主模式）+ JWT 占位 |
| `prompts/` | 提示词模板（aiofiles 异步加载） |

## 6. 关键设计约定

- **全异步**：IO 操作（LLM 调用、文件读写、MinIO）必须 async；CPU 密集（PDF 解析、embedding 编码）用 `run_in_executor`
- **单例统一**：服务单例继承 `AsyncSingleton`，统一异步初始化/清理生命周期
- **可降级**：混合检索失败回退 dense；联网搜索失败不影响知识库回答；工具失败降级为 LLM 直答
- **输出净化**：答案不携带推理前缀、不整段复述原文，关键事实标注 `[n]` 来源编号
- **离线优先**：所有模型本地部署，禁用付费 API

## 7. 相关文档

- API 细节：[api.md](api.md)
- 部署：[guide/deployment.md](guide/deployment.md)
- 开发与测试：[guide/development.md](guide/development.md)
- 监控运维：[guide/monitoring.md](guide/monitoring.md)
- 后续优化：[design/improvement-roadmap.md](design/improvement-roadmap.md)
- 专项设计：见 `design/` 目录
