# 模型 Provider 注册与任务路由（2026-10）

> 状态：方案已确认，P1 实施中  
> 目标：让知澜支持本地 Ollama、OpenAI-Compatible API、Anthropic / Gemini / Azure 等模型服务，按任务路由，支持热切换与降级，同时不破坏现有 Ollama 部署。  
> 关联：[project-status.md](../project-status.md)、[chat-latency-and-model-config.md](chat-latency-and-model-config.md)、[architecture.md](../architecture.md)

---

## 1. 结论与边界

- **聊天模型可以热切换**：更换回答/直答/轻量任务模型不需要重启。
- **Embedding 模型不能直接热换**：向量维度或口径变化必须重建 Milvus 索引；语义缓存、意图分类、Wiki 路由、引用补全共用同一 embedding。
- **Reranker 可以热换**，但需要重新评估检索质量。
- 首期优先支持 **Ollama 原生 + OpenAI-Compatible**；后者覆盖 OpenAI、DeepSeek、通义兼容模式、Kimi、智谱、MiniMax、SiliconFlow、OpenRouter、vLLM、LM Studio、llama.cpp server 等。
- Anthropic / Gemini / Azure 原生适配放到后续阶段。

---

## 2. 现状与耦合点

当前 `ModelManager` 已有任务角色/降级链雏形，但仍是 Ollama 专用：

| 位置 | 现状 |
|---|---|
| `services/model_manager.py` | `get_chat_llm()` 返回 `ChatOllama`；`get_embeddings()` 返回 `OllamaEmbeddings` |
| `services/rag_chain.py` | 直接构造 `ChatOllama` 主模型 / 直答模型 |
| `services/title_generator.py` | 直接构造 `ChatOllama` |
| `services/wiki_compiler.py` | 直接构造 `ChatOllama` |
| `services/milvus_service.py` | 类型与初始化绑定 Ollama Embeddings |
| `services/citation_backfiller.py`、`semantic_cache_service.py`、`intent_router/embedding_classifier.py`、`strategies/semantic.py` | 通过 `model_manager.get_embeddings()` 使用 Ollama |
| `hybrid_search.py`、`web_search_service.py` | 本地 `sentence-transformers CrossEncoder`，Ollama Embeddings 兜底 |
| `config.py` | `OLLAMA_*` 模型名、`OLLAMA_HOST`、`OLLAMA_SUPPORTS_THINKING`、`OLLAMA_NUM_CTX` |

因此需要新增 **Provider / Model / Route** 三层，业务层只依赖统一工厂。

---

## 3. 目标架构

### 3.1 三层模型

| 层 | 职责 |
|---|---|
| Provider | 一个可连接的服务：Ollama、OpenAI-Compatible、Anthropic、Gemini、Azure |
| Model | Provider 下的具体模型：能力、上下文、输出上限、Embedding 维度、成本 |
| Route | 系统任务到 Model 的映射：primary、fallbacks、参数、能力要求 |

### 3.2 适配器接口

```text
ChatProvider
  - invoke(messages, tools, reasoning, params)
  - stream(messages, tools, reasoning, params)
  - bind_tools(tools)
  - health_check()
  - list_models()
  - capabilities()

EmbeddingProvider
  - embed_documents(texts)
  - embed_query(text)
  - dimension

RerankProvider
  - rerank(query, documents, top_k)
```

P1 只实现：

- `OllamaChatProvider`
- `OpenAICompatibleChatProvider`
- 现有 Ollama Embeddings 继续通过路由读取，OpenAI/Cohere Embedding 放 P3

业务代码返回 LangChain `BaseChatModel` / `Embeddings` 兼容对象，避免一次性重写 Agent / 管线 / 评测调用点。

### 3.3 任务路由

| Task | 用途 | 能力要求 |
|---|---|---|
| `answer` | 深度思考开启时的主回答 | chat + stream；可选 reasoning |
| `answer_direct` | 深度思考关闭时的直答 | chat + stream |
| `fast` | 意图、标题、改写、校验 | chat |
| `intent_router` | 意图分类 | chat |
| `title_generation` | 会话标题 | chat |
| `query_rewrite` | Query 改写 | chat |
| `verifier` | 事实校验 | chat |
| `agent_decision` | Agent 工具决策 | chat + tools |
| `embedding` | 检索/语义缓存/意图分类 | embedding |
| `rerank` | 知识库与网页重排 | rerank |
| `wiki_compile` | Wiki 编译 | chat |
| `doc_analyze` | 文档分类/质量分析 | chat |

路由示例：

```json
{
  "answer": {
    "primary": "qwen3-4b",
    "fallbacks": ["gpt-4.1-mini"],
    "reasoning": "native"
  },
  "answer_direct": {
    "primary": "qwen25-3b",
    "fallbacks": ["gpt-4.1-mini"]
  },
  "fast": {
    "primary": "qwen25-3b",
    "fallbacks": []
  }
}
```

---

## 4. P1 配置格式

P1 使用 **JSON 配置文件 + 环境变量兜底**，不引入数据库与 UI；P2 再迁移到 DB + 管理界面。

- 运行时文件：`backend/data/model_registry.json`（不入库）
- 示例文件：`backend/configs/model_registry.example.json`
- 覆盖路径：`MODEL_REGISTRY_FILE`
- 没有文件时：按现有 `OLLAMA_*` 自动生成 legacy registry，行为不变
- API Key 只写 `api_key_env`（环境变量名）或 `api_key`（仅本地开发），不在 GET 接口返回

```json
{
  "version": 1,
  "providers": [
    {
      "id": "ollama-local",
      "type": "ollama",
      "base_url": "http://host.docker.internal:11434",
      "enabled": true
    },
    {
      "id": "openai",
      "type": "openai_compatible",
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "OPENAI_API_KEY",
      "enabled": false
    }
  ],
  "models": [
    {
      "id": "qwen3-4b",
      "provider": "ollama-local",
      "model": "qwen3:4b",
      "display_name": "本地推理模型",
      "capabilities": ["chat", "stream", "tools", "reasoning"],
      "context_window": 8192
    },
    {
      "id": "qwen25-3b",
      "provider": "ollama-local",
      "model": "qwen2.5:3b-instruct",
      "display_name": "本地直答模型",
      "capabilities": ["chat", "stream", "tools"],
      "context_window": 6144
    },
    {
      "id": "gpt-4.1-mini",
      "provider": "openai",
      "model": "gpt-4.1-mini",
      "display_name": "外部推理模型",
      "capabilities": ["chat", "stream", "tools", "vision"],
      "context_window": 128000
    }
  ],
  "routes": {
    "answer": {"primary": "qwen3-4b", "fallbacks": ["gpt-4.1-mini"]},
    "answer_direct": {"primary": "qwen25-3b", "fallbacks": ["gpt-4.1-mini"]},
    "fast": {"primary": "qwen25-3b", "fallbacks": []}
  }
}
```

---

## 5. 运行时行为

### 5.1 能力校验

保存/加载时校验：

- 任务需要 tools → 模型必须有 `tools`
- 深度思考开启 → 模型必须有 `reasoning`（否则禁用开关或走提示词推理）
- Embedding 模型必须有 `dimension`
- 上下文窗口 + 输出上限必须满足当前 RAG 预算

不满足时拒绝加载并记录明确错误，不在运行时静默失败。

### 5.2 热切换

- registry 有 `version`；文件 mtime/内容变化时重载
- `ModelManager` 按 `(provider, model, params)` 缓存实例
- 新请求使用新配置；在途请求继续使用旧实例
- P2 增加管理员 API/UI 后，通过配置版本号触发同样机制

### 5.3 降级

- Provider 级健康检查与熔断（429 / 5xx / timeout）
- Route fallbacks 依次尝试
- 记录 `fallback_reason` 到 trace
- `allow_external_fallback` 控制本地 → 外部 API 的降级，避免数据出域

### 5.4 思考与工具归一化

- reasoning：Ollama `think`、OpenAI `reasoning_effort` / 模型能力、Anthropic thinking 等由适配器映射
- tools：OpenAI tools、Ollama tools；不支持 tools 的模型禁止进入 Agent 路由
- 不支持 reasoning 的模型：深度思考开关显示为不可用，不伪造思考链
- 流式统一为 `content / thinking / tool_calls / usage` 事件，SSE 协议不变

---

## 6. 安全与运维

- API Key 加密存储（P2）；P1 文件只允许 `api_key_env`，禁止提交明文
- Key 永不返回前端、永不进日志/trace
- Provider/Route 修改需要 `require_admin` + 审计
- 远程 Provider 标记 `external`，Base URL 做 SSRF/域名校验
- 用量统计：provider/model/task、tokens、延迟、错误、估算成本
- 成本上限与 429 告警

---

## 7. Embedding / Reranker 迁移

Embedding 切换按“数据迁移”处理：

1. 记录 `embedding_profile_id + dimension` 到 collection 元数据
2. 维度变化：
   - 安全模式：禁止直接切换，要求重建索引
   - 双写模式（后续）：新 collection 回填，完成后切别名
3. 语义缓存、意图分类、Wiki 路由、引用补全同步失效
4. 重建失败自动回滚
5. Reranker 可热换，但需跑离线检索评估

P1 不改 Embedding 的 Milvus schema，只把构造入口收敛到 registry；P3 做多 Embedding 迁移。

---

## 8. 管理界面（P2）

设置 → 模型服务（仅管理员）：

- Provider 列表：新增/编辑/测试连接/拉取模型
- 模型列表：能力、上下文、维度、成本
- 路由表：primary + fallbacks + 能力校验 + 测试请求
- 保存并热加载
- 用量/错误/延迟/成本
- Embedding 维度变化警告与重建入口

普通用户仍只看到“本地推理/直答模型”，不暴露厂商模型名。

---

## 9. 阶段与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 触点盘点、接口与能力矩阵、文档 | 本文档 + 任务台账 |
| P1 | JSON registry + Ollama/OpenAI-Compatible 适配器 + 路由 + env 兜底 + 热重载 | 本地/外部聊天模型可切换；旧 Ollama 配置不改也能启动；能力校验与 fallback 单测通过 |
| P2 | Provider/Route 管理 API + UI + Key 加密 + 测试连接 + 用量 | 管理员 UI 可添加/切换/回滚；Key 不回显 |
| P3 | Embedding/Reranker 抽象 + 索引重建 + 缓存失效 | 维度变化必须重建；重建可回滚 |
| P4 | Anthropic/Gemini/Azure 原生 + 租户/成本策略 | 按需 |

### P1 验收标准

- 配置了 OpenAI-Compatible Provider 后，可把 `answer` 或 `answer_direct` 路由到外部模型
- 不配置新文件时，`OLLAMA_*` legacy 自动生成路由，行为与当前一致
- 缺少 tools 的模型不能路由到 `agent_decision`
- 主模型 429/超时可按 fallback 切换，trace 记录原因
- 新增 registry/route/adapter 单测，现有后端测试不回归
- 前端不需要改动，普通用户仍只看到产品化模型名称

---

## 10. 主要风险

| 风险 | 处理 |
|---|---|
| 外部 API 数据出域 | Provider 标记 external；route 控制 fallback；后续租户策略 |
| Embedding 维度错配 | P1 不改维度；P3 强制重建 |
| 工具调用能力差异 | 能力声明 + 路由校验 + prompt JSON 降级 |
| 思考语义不一致 | 适配器归一化；不支持则禁用开关 |
| API Key 泄漏 | P1 只用 env 名称；P2 加密 + write-only |
| Provider 限流/成本 | 熔断、fallback、用量与预算告警 |
| 多厂商输出质量漂移 | 保留离线评测集，切换后跑回归 |

---

## 11. P1 实施记录（2026-10-25）

### 11.1 已落地

- 新增 `backend/src/services/model_registry.py`：Provider/Model/Route Pydantic 配置、legacy Ollama 自动 bootstrap、能力校验、跨 Provider fallback 过滤。
- 新增 `backend/src/services/model_adapters.py`：
  - Ollama ChatModel / Embeddings
  - OpenAI-Compatible ChatModel / Embeddings（`langchain-openai`）
  - `ChatModelGroup`：多模型 `with_fallbacks`，并保留 `bind_tools`，兼容 Agent 决策
- `ModelManager` 改为 registry 入口：
  - `get_chat_llm()` 返回路由模型组
  - `get_embeddings()` 走 embedding route（维度不一致直接拒绝）
  - 文件 mtime 变化自动重载；坏文件保留上一版快照
  - `RAGChain` 请求入口按 registry 代次刷新 answer/direct/fast/agent 模型引用；TitleGenerator/WikiCompiler 缓存同样按代次失效
- 接管直接构造点：`RAGChain`、`TitleGenerator`、`WikiCompiler`；`AnswerGenerator` / pipeline generate 不再业务层 `bind(reasoning=...)`。
- `agent_decision` 独立路由：联网搜索/Agent 决策使用工具能力校验后的模型，避免 fast 路由不支持 tools 时失败。
- 启动校验：有 registry 文件时按注册表校验；无文件时保持原 `OLLAMA_*` 校验与行为。
- 新增 `backend/configs/model_registry.example.json` 与 `.env.example` 的 `MODEL_REGISTRY_FILE` / `OPENAI_API_KEY` 说明。

### 11.2 P1 验证

- 全量后端单测：**1131 passed / 105 skipped**（排除 Windows python-magic 已知模块）
- 新增 registry / P2 服务测试：15 passed
- 真实链路（legacy Ollama）：`get_chat_llm("fast")` → `ChatModelGroup` → Ollama 返回正常
- OpenAI-Compatible 适配器：无网络构造 `/v1` ChatOpenAI 成功；缺 Key 时明确报错
- 自定义 registry + 本地 mock `/v1/chat/completions` 端到端：路由到 mock 模型并返回 `mock-ok`

### 11.3 P1 限制

- 配置目前是 **JSON 文件 + env**，管理 UI / 加密 Key / DB 持久化为 P2。
- Runtime fallback 依赖 LangChain `with_fallbacks`，尚未加入 provider 级熔断和跨请求健康状态（P2）。
- Embedding 仅收敛入口与维度校验，未实现多 collection 双写/重建流程（P3）。
- 开发容器需要重建以安装新增的 `langchain-openai` 依赖。

---

## 12. P2 实施记录（2026-10-26）

### 12.1 DB 与管理 API

- 新增表：`model_providers` / `model_deployments` / `model_routes`，迁移 `f2b3c4d5e6f7`。
- Provider API Key 使用 Fernet 加密（`MODEL_SECRET_ENC_KEY`，未配置时从 `SECRET_KEY` 派生并告警）；GET 仅返回 `has_api_key` / `api_key_hint`。
- 新增管理员 API `/api/admin/models/*`：Provider/模型 CRUD、连接测试、模型发现、路由全量保存、校验并应用、Registry 快照。
- 启动顺序：DB 有 Provider 配置则优先加载；否则回退 P1 文件/legacy；启动后 `alembic upgrade head` + `alembic check` 已验证无 drift。

### 12.2 前端管理页

- 设置 → 模型服务（`/settings/models`）：
  - Provider 列表、新增/编辑/删除、连接测试、Key 只写
  - 模型列表、能力声明、上下文/输出/Embedding 维度
  - 任务路由（primary + fallbacks + 外部降级开关）
  - “校验并应用”热更新在线 registry
  - 生产环境管理员密钥 `X-Admin-Key` 输入（sessionStorage，不持久化到 localStorage）

### 12.3 验证

- 后端全量单测：**1131 passed / 105 skipped**
- 新增 P2 加密/管理 API/DB→registry 测试：4 passed
- 前端 lint / typecheck / 131 单测 / build：通过
- 临时 PostgreSQL 容器：`alembic upgrade head` + `alembic check` → **No new upgrade operations detected**

### 12.4 P2 限制

- 用量/成本统计、Provider 级熔断/健康缓存尚未实现（P2.1）。
- 管理页当前为全局配置；租户级 Provider/路由属于 P4。
- Embedding 跨模型迁移、多 collection 重建仍属 P3。
- 外部 Provider 的 Key 推荐通过 `api_key_env`；管理页保存的 Key 依赖 `MODEL_SECRET_ENC_KEY` 稳定，换 key 后需重新录入。
