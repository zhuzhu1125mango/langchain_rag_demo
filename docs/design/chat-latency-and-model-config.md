# 问答链路首字延迟治理与模型配置去硬编码方案

> 状态：已实施（2026-09-07），实施记录见文末 §10；2026-10-03 二次优化与瓶颈诊断见 §11，直答模型 A/B 实测与定稿见 §12，2026-10-12 后续优化实测（多轮慢例修复 / reranker 换型 / 天气提取器修复）见 §13
> 后续演进：模型选择已由 [model-provider-registry-2026-10.md](model-provider-registry-2026-10.md) 的 Provider/Model/Route registry 接管（P1 2026-10-25、P2 2026-10-26）；本篇保留历史决策、延迟治理与 A/B 记录，`.env` 模型配置仅作 legacy 兜底
> 范围：后端问答管线（backend/src）、前端 ChatView 事件处理（frontend/src）
> 约束：离线开源模型（Ollama）；模型名不得硬编码，一律走配置；深度思考仅保留 开/关 两态；回答须结合知识库检索与模型自身智能

---

## 1. 背景与目标

用户反馈：前端发送问题后，需要很久才能看到回复。经对代码的实际核查，慢的根因集中在**第一个正文 token 产出之前**，管线串行执行了多次本地模型调用，其中重排序对全部候选逐条打分、辅助任务未关闭思考模型（qwen3）的思考链、以及标题生成阻塞结束事件。同时代码中存在模型名硬编码默认值，与"模型由配置决定"的要求冲突。

目标：

1. 模型名零硬编码，全部由 `.env` 配置提供，启动时校验；
2. 首字延迟显著下降（目标：选知识库问答场景从当前数十秒量级降到 10 秒内）；
3. 所有优化点带独立开关，可逐项回滚；SSE 协议只增不改，旧客户端不受影响。

---

## 2. 现状调研（均已对照代码核实）

### 2.1 深度思考开关现状：仅 开/关，无 auto（已核实）

| 层 | 事实 | 位置 |
|---|---|---|
| 前端 UI | 布尔 pill 开关，`deepThinking = ref(false)` | [ChatView.vue L144](../../frontend/src/views/ChatView.vue#L144)、[ChatInputArea.vue L235](../../frontend/src/components/chat/ChatInputArea.vue#L235) |
| 前端请求 | 只发送 `'on' / 'off'`：`deep_thinking: deepThinking.value ? 'on' : 'off'` | [ChatView.vue L429](../../frontend/src/views/ChatView.vue#L429) |
| 后端校验 | `allowed = {"on", "off"}`，默认 `"off"` | [api/chat.py L47-57](../../backend/src/api/chat.py#L47-L57) |
| 后端决策 | `OLLAMA_SUPPORTS_THINKING=false` → None（不干预）；否则 `deep_thinking == "on"` | [rag_chain.py L120-129](../../backend/src/services/rag_chain.py#L120-L129) |
| 思考绑定 | 主回答链 `llm.bind(reasoning=think)`，think 为 None 时不绑定 | [rag_chain.py L1371](../../backend/src/services/rag_chain.py#L1371) |

**残留问题**：[rag_chain.py L119](../../backend/src/services/rag_chain.py#L119) 注释仍写"auto 模式下判定需要深度思考的提示词"，为过时注释，需清理。前端 `chat.ts` 已无流式/思考相关类型（SSE 请求在 ChatView 内联构造），无 `'auto'` 类型残留。

> 结论：本方案不再涉及任何 auto 启发式逻辑，仅清理过时注释。

### 2.2 当前模型配置（.env 实际值）

| 配置项 | 当前值 | 说明 |
|---|---|---|
| `OLLAMA_MODEL_NAME` | `qwen3:4b` | 主回答模型（思考模型，`deep_thinking=on` 专属） |
| `OLLAMA_DIRECT_MODEL_NAME` | `qwen2.5:3b-instruct` | 关闭深度思考时的直答专用模型（2026-10-03 切换） |
| `FAST_LLM_MODEL_NAME` | `qwen2.5:3b-instruct` | 辅助任务模型；须与 `OLLAMA_DIRECT_MODEL_NAME` 一致，避免同请求内多模型换载 |
| `EMBEDDING_MODEL_NAME` | `bge-m3:latest` | 嵌入模型（1024 维） |
| `KB_RERANK_MODEL` / `KB_RERANK_PROVIDER` | `BAAI/bge-reranker-base` / `sentence_transformers` | 知识库重排序（2026-10-03 由 v2-m3 换型） |
| `OLLAMA_SUPPORTS_THINKING` | `true` | qwen3 支持思考 |

**2026-10-03 更新**：直答与辅助任务已切换为 `qwen2.5:3b-instruct`（原生非思考），与主模型 `qwen3:4b` 分属不同常驻角色——下文 §2.2 原「主辅同名、无换载」的结论已不再适用；显存约束改由「4B 仅 `deep_thinking=on` 主回答链，其余全部走 3B」保证（详见 [project-status.md §四](../project-status.md) 与 [direct-model-switch-plan-2026-10-03.md](../archive/direct-model-switch-plan-2026-10-03.md)）。

**2026-10-26 更新**：上表为 legacy 模式默认值；P1/P2 起可经 DB/JSON registry 按任务覆盖（配置优先级与切换方式见 [model-provider-registry-2026-10.md](model-provider-registry-2026-10.md) §4/§8）。

### 2.3 模型名硬编码点清单

| 位置 | 硬编码内容 | 影响 |
|---|---|---|
| [config.py L103-136](../../backend/src/config.py#L103-L136) | 默认值 `bge-m3:latest`、`deepseek-r1:7b-qwen-distill-q4_K_M`、`qwen2.5:7b`、`qllama/bge-reranker-v2-m3:latest` | .env 缺项时静默使用过时模型 |
| [api/config.py L30-33](../../backend/src/api/config.py#L30-L33) | `ModelConfig` 响应字段默认 deepseek/qwen2.5 | 配置接口可能泄露硬编码名（实际被 settings 覆盖，属脏默认） |
| `.env.example`、`.env.prod` | 原为 deepseek-r1:7b / qwen2.5:7b，**已于 2026-10-15 修正**为 qwen3:4b / qwen2.5:3b-instruct | 已解决（原影响：误导新部署） |
| 过时注释 | [rag_chain.py L119](../../backend/src/services/rag_chain.py#L119)、search_agent.py、citation_backfiller.py 中 "deepseek-r1" 字样 | 文档性误导 |

### 2.4 模型实例化点清单（11 处构造点）

| 服务 | 构造方式 | 模型来源 | 问题 |
|---|---|---|---|
| 主回答链 [rag_chain.py L246-247](../../backend/src/services/rag_chain.py#L246-L247) | `ChatOllama` | model_manager `answer` 角色 | 正常 |
| 标题生成 [title_generator.py L40](../../backend/src/services/title_generator.py#L40) | `ChatOllama` | model_manager `title_generation` | **未关思考** |
| 意图路由 LLM [llm_router.py L115](../../backend/src/services/intent_router/llm_router.py#L115) | `ChatOllama` | model_manager `intent_router` | **未关思考** |
| 生成评估器 [generation_evaluator.py L56](../../backend/src/services/evaluation/generation_evaluator.py#L56) | `ChatOllama` | model_manager `fast` | **未关思考** |
| 策略投票 [llm_inference.py L36-37](../../backend/src/services/strategies/llm_inference.py#L36-L37) | `ChatOllama` **直接读 settings** | `FAST_LLM_MODEL_NAME` | 不走 model_manager（无降级）、**未关思考**、调用**无超时** |
| 查询改写 [query_rewriter.py L108](../../backend/src/services/query_rewriter.py#L108) | `ChatOllama` **直接读 settings** | `FAST_LLM_MODEL_NAME` | `__init__` 同步构造、**未关思考** |
| 指代消解/摘要 [context_enhancer.py](../../backend/src/services/context_enhancer.py) | 注入 `self.llm` | **主回答模型**（[rag_chain.py L288](../../backend/src/services/rag_chain.py#L288) `ContextEnhancer(self.llm)`） | 用主模型做小任务、**未关思考**、每轮必调 |
| 联网搜索 [web_search_service.py L256 注入](../../backend/src/services/rag_chain.py#L256) | `WebSearchService(llm=self.llm)` | **主回答模型** | 查询改写用主模型；embeddings/reranker 自建（L289、L317） |
| Milvus 嵌入 [milvus_service.py L61](../../backend/src/services/milvus_service.py#L61) | `OllamaEmbeddings` | settings | 与其他 embeddings 实例重复构造 |
| 意图分类嵌入 [embedding_classifier.py L74](../../backend/src/services/intent_router/embedding_classifier.py#L74) | `OllamaEmbeddings` | settings | 重复构造 |
| 检索评估嵌入 [retrieval_evaluator.py L64](../../backend/src/services/evaluation/retrieval_evaluator.py#L64) | `OllamaEmbeddings` | settings | 重复构造（低频） |

另有体系外依赖：[rag_chain.py L308](../../backend/src/services/rag_chain.py#L308) 与 [strategies/semantic.py](../../backend/src/services/strategies/semantic.py) 使用 HuggingFace `sentence-transformers/all-MiniLM-L6-v2`（非 Ollama 体系，离线环境可能不可用）。

### 2.5 问答链路与"首字前"阻塞点

流式管线 9 个阶段全串行（[rag_chain.py `_pipeline` L727 起](../../backend/src/services/rag_chain.py#L727)），第一个 `content` 事件要等阶段 9。首字前的模型调用：

| 顺序 | 阶段 | 模型调用 | 代码位置 |
|---|---|---|---|
| 1 | 上下文增强 | 指代消解 LLM 非流式调用，**有历史对话即触发** | [context_enhancer.py `_resolve_references`](../../backend/src/services/context_enhancer.py) |
| 2 | 意图路由 | Embedding 分类 1 次编码；规则未命中时 LLM 路由（`wait_for` 超时 3s） | [llm_router.py](../../backend/src/services/intent_router/llm_router.py)、[config.py L159](../../backend/src/config.py#L159) |
| 3 | KB 决策 | 选 KB 且未开联网 → `HYBRID_INTELLIGENT` → 四策略投票，其中 LLM 策略 `ainvoke` **无超时** | [decision_pipeline.py L140-149](../../backend/src/services/decision_pipeline.py#L140-L149)、[llm_inference.py L79](../../backend/src/services/strategies/llm_inference.py#L79) |
| 4 | KB 检索 | dense+sparse 各取 20 条 → RRF 融合（最多 40 条）→ **rerank 对全部 40 条逐条打分** | [milvus_service.py L536-547](../../backend/src/services/milvus_service.py#L536-L547)、[hybrid_search.py L142-144](../../backend/src/services/hybrid_search.py#L142-L144)、[ollama_reranker.py L96-108](../../backend/src/services/ollama_reranker.py#L96-L108) |
| 5 | 相关性复算 | MiniLM 对问题+3 文档 CPU 编码 4 次 | [rag_chain.py L308、L313、L1141](../../backend/src/services/rag_chain.py#L308) |
| 6 | 生成回答 | 主模型流式；思考开启时 reasoning 先出，**思考结束后才有首个 content** | [rag_chain.py L1243-1244、L1371](../../backend/src/services/rag_chain.py#L1243) |
| 收尾 | 持久化 | **标题生成在 end 事件之前被 await**，标题用 LLM 非流式生成 | [chat.py L324-331、L441-461](../../backend/src/api/chat.py#L324-L461) |

已有但未覆盖上述问题的机制：启动时主模型预热（[main.py L180-187](../../backend/src/main.py#L180-L187)）、模型可用性 fallback 链（[model_manager.py](../../backend/src/services/model_manager.py)）、管线各阶段耗时 Trace（request_traces 表 / Trace 页面）。

---

## 3. 问题分析（按影响排序）

### P1 重排序对全部候选逐条调用 Ollama（最大延迟点）

`search_hybrid` 中 dense/sparse 各取 `KB_HYBRID_SEARCH_TOP_K=20`（[config.py L130](../../backend/src/config.py#L130)），RRF 融合后最多 40 条**全部**传入 rerank；[KBReranker.rerank](../../backend/src/services/hybrid_search.py#L122-L157) 对每条候选构造 pair 调 `OllamaReranker.predict`，后者用 `asyncio.gather` 并发发起 40 次 `client.generate`（[ollama_reranker.py L96-108](../../backend/src/services/ollama_reranker.py#L96-L108)）。Ollama 单实例串行排队，每次请求都要 prefill 整个文档块，40 次累计可达数十秒。配置项 `KB_HYBRID_RERANK_TOP_K=5` 只用于打分**之后**的截断，未减少打分次数。

### P2 辅助任务未关闭 qwen3 思考链

意图路由（输出 JSON）、策略投票（YES/NO）、标题生成、指代消解、查询改写均为结构化小任务，但它们的 `ChatOllama` 实例都没有 `bind(reasoning=False)`。qwen3 在 `OLLAMA_SUPPORTS_THINKING=true` 下默认思考，每个辅助调用都要先生成一段思考链，单次增加数秒到十几秒。

### P3 指代消解每轮必调且使用主模型

第二轮对话起，[ContextEnhancer](../../backend/src/services/context_enhancer.py) 无条件用主回答模型做非流式指代消解；多数后续问题并不包含"它/这个/上面"等指代词，属无效调用。项目中已存在轻量规则版指代处理（意图路由的 ConversationContextBuilder），可前置复用。

### P4 标题生成阻塞 end 事件

标题生成位于 `save_assistant_message` 内部（[chat.py L324-331](../../backend/src/api/chat.py#L324-L331)），而该协程在发送 `end` 事件前被 `await`（[chat.py L441-461](../../backend/src/api/chat.py#L441-L461)）。正文已全部送达，但用户要等标题生成（又一次 LLM 调用）后才看到结束态，表现为"回答完了还在转圈"。

### P5 策略投票 LLM 无超时、显式选 KB 仍投票

- `llm_inference.analyze` 的 `ainvoke` 无超时（[llm_inference.py L79](../../backend/src/services/strategies/llm_inference.py#L79)），模型异常时拖慢整个决策阶段；
- 用户显式勾选知识库后仍走四策略投票决定 `should_use_kb`（[decision_pipeline.py L140-149](../../backend/src/services/decision_pipeline.py#L140-L149)），投票否决时不检索知识库，与用户显式意图可能相悖。回答模板本身已保证"参考信息不足时结合模型自身知识回答"，跳过投票不改变回答质量约束。

### P6 MiniLM 重复相关性计算 + 体系外依赖

检索结果已带 dense/rrf/rerank 分数，[_calculate_relevance](../../backend/src/services/rag_chain.py#L313-L367) 又用 `all-MiniLM-L6-v2` 对问题和 3 个文档重新编码（4 次 CPU 编码），功能与 rerank 重复；semantic 策略依赖同一 HF 模型，离线环境加载失败时该策略永久弃权。

### P7 意图路由 LLM 超时偏紧

`INTENT_ROUTER_LLM_TIMEOUT=3.0`（[config.py L159](../../backend/src/config.py#L159)），模型冷启动或思考未关闭时 3 秒必然超时 → 规则未命中时每次白等 3 秒，且 Ollama 侧请求不会因客户端超时停止，继续占用模型。

### P8 模型名硬编码

见 2.3，默认值与示例环境文件仍指向 deepseek-r1/qwen2.5，换模型只能靠 .env 覆盖且无启动校验，配错时首次调用才暴露。

---

## 4. 修改方案

### A. 模型配置去硬编码

**A1. config.py 默认值中性化**（[config.py L102-136](../../backend/src/config.py#L102-L136)）

- `OLLAMA_MODEL_NAME`、`FAST_LLM_MODEL_NAME`、`EMBEDDING_MODEL_NAME` 默认改为 `Optional[str] = None`，由 `.env` 必填；
- `KB_RERANK_MODEL`、`SEARCH_RERANK_MODEL` 默认 `None`：为 None 时对应 rerank 自动关闭（按 RRF/原文顺序截断），不再隐式拉取用户未声明的模型；
- `OLLAMA_SUPPORTS_THINKING` 保留，注释更新为"按所用模型能力设置（qwen3、deepseek-r1 等思考模型设 true；qwen2.5 等设 false）"。

**A2. 启动校验**（[main.py lifespan](../../backend/src/main.py#L130)）

- 预热阶段前增加模型配置校验：三项模型名非空；通过 Ollama `/api/tags` 确认主模型、fast 模型、embedding 模型、rerank 模型（如配置）存在；缺失或不可用时**启动失败**并输出中文错误（列出缺失项与 .env 配置名）。开发/生产一致执行（模型是核心依赖，不同于 SECRET_KEY 的环境差异）。

**A3. 配置接口脏默认清理**（[api/config.py L30-33](../../backend/src/api/config.py#L30-L33)）

- `ModelConfig` 字段默认值改为空串/0，值始终由 settings 填充。

**A4. 环境文件与注释**

- `.env.example`、`.env.prod` 模型行改为占位 + 示例注释（注明 embedding 维度需与模型匹配：bge-m3=1024、nomic-embed-text=768）；
- 清理 rag_chain.py L119、search_agent.py、citation_backfiller.py 中 "deepseek-r1" 过时注释，改为"思考类模型"的中性表述。

### B. 统一模型工厂（model_manager）

在 [model_manager.py](../../backend/src/services/model_manager.py) 集中构造，业务代码不再直接 `ChatOllama(...)` / `OllamaEmbeddings(...)`：

**B1. 新增 `async get_chat_llm(task, *, streaming=False, think=False, num_ctx=None, timeout=None, temperature=None)`**

- 模型名走现有 task role + fallback 链（answer / fast / intent_router / title_generation / query_rewrite / context / evaluation）；
- 统一注入 `num_ctx`；
- **think 显式绑定**：`think` 为 True/False 且 `OLLAMA_SUPPORTS_THINKING=true` 时返回 `llm.bind(reasoning=think)`（与 [rag_chain.py L1371](../../backend/src/services/rag_chain.py#L1371) 现有写法一致）；辅助任务一律 `think=False`；主回答任务由 `should_think()` 结果传入；
- timeout 由调用方按需传入。

**B2. 新增 `async get_embeddings()` 单例**

- milvus_service、embedding_classifier、web_search_service、retrieval_evaluator 共享同一 `OllamaEmbeddings` 实例；模型名变化时单例失效重建。

**B3. 改造点**

| 文件 | 改造 |
|---|---|
| [strategies/llm_inference.py](../../backend/src/services/strategies/llm_inference.py) | 改调 `get_chat_llm("fast", think=False, timeout=STRATEGY_LLM_TIMEOUT)`，删除直接构造 |
| [query_rewriter.py](../../backend/src/services/query_rewriter.py) | 同步构造改为异步懒加载 `get_chat_llm("query_rewrite", think=False)` |
| [context_enhancer.py](../../backend/src/services/context_enhancer.py) | 改为接收 fast 任务 llm（`think=False`），由 rag_chain 初始化时通过工厂获取注入 |
| [web_search_service.py](../../backend/src/services/web_search_service.py) | 查询改写改用 fast llm；embeddings 走共享单例 |
| [llm_router.py L115](../../backend/src/services/intent_router/llm_router.py#L115)、[title_generator.py L40](../../backend/src/services/title_generator.py#L40)、[generation_evaluator.py L56](../../backend/src/services/evaluation/generation_evaluator.py#L56) | 构造统一走工厂并 `think=False` |
| [milvus_service.py L61](../../backend/src/services/milvus_service.py#L61)、[embedding_classifier.py L74](../../backend/src/services/intent_router/embedding_classifier.py#L74)、retrieval_evaluator | embeddings 走共享单例 |
| [rag_chain.py L288](../../backend/src/services/rag_chain.py#L288) | `ContextEnhancer` 注入 fast llm |

question_processor / kb_comparator / document_analyzer / knowledge_graph_generator 为低频功能，本次不改。

### C. 首字延迟治理（每项独立开关）

**C1. Rerank 候选截断（P1，收益最大）**

- [milvus_service.py L546](../../backend/src/services/milvus_service.py#L546)：RRF 融合后先截断到 `KB_HYBRID_RERANK_TOP_K`（默认 5）再送打分：`rerank_results(query, fused[:rerank_n], top_k=k)`；
- 新增 `KB_RERANK_ENABLED=true`：关闭或 rerank 模型未配置时，直接按 RRF 分数截断（现有降级路径已支持）；
- 联网搜索 rerank 同步核对候选截断（`SEARCH_RERANK_TOP_K`）。

**C2. 辅助任务全部关闭思考（P2）**

- 由 B1 工厂统一保证：意图路由、策略投票、标题、查询改写、指代消解、摘要压缩、评估器全部 `think=False`；
- 主回答链行为不变（on/off 开关 → `should_think()` → `bind(reasoning=...)`）。

**C3. 指代消解规则前置（P3）**

- [context_enhancer._resolve_references](../../backend/src/services/context_enhancer.py)：进入 LLM 调用前先用指代词正则（复用 intent_router/constants.py 的 `PRONOUN_PATTERN`：它/这个/那个/该/上面/刚才…）判断；**无代词或无历史 → 直接返回原问题，零 LLM 调用**；
- 阶段 1 默认改用规则版上下文补全（ConversationContextBuilder 已有逻辑），LLM 版降级为兜底，新增开关 `CONTEXT_RESOLVE_USE_LLM=false`；
- `_compress_context`（仅历史超 2000 字触发）同步使用 fast llm + `think=False`。

**C4. 策略投票 LLM 超时（P5）**

- [llm_inference.py](../../backend/src/services/strategies/llm_inference.py)：`ainvoke` 包 `asyncio.wait_for`，新增 `STRATEGY_LLM_TIMEOUT=5.0`；超时/异常按弃权处理（置信度 0.3，不参与投票），不阻塞决策。

**C5. 显式选 KB 时跳过策略投票（P5）**

- 新增 `DECISION_VOTE_WHEN_KB_SELECTED=false`：用户勾选知识库时，[DecisionPipeline](../../backend/src/services/decision_pipeline.py#L140-L149) 直接 `should_use_kb=True`（模式仍为 HYBRID，回答模板已保证结合模型自身知识）；投票可异步执行，结果仅记录到学习引擎，不阻塞响应；
- 未选 KB 时逻辑不变（本就 PURE_LLM 直答，不触发投票）。

**C6. 意图路由超时调整（P7）**

- `INTENT_ROUTER_LLM_TIMEOUT` 默认 3.0 → 5.0（C2 关闭思考后 qwen3:4b 的 JSON 分类通常 1~3 秒可完成；冷启动场景 3 秒偏紧）；降级逻辑不变。

**C7. 标题生成移出关键路径（P4）**

- [chat.py](../../backend/src/api/chat.py#L435-L461)：`end` 事件先发送（不含 title）；标题生成改为 `asyncio.create_task` 后台执行，完成后经同一 SSE 连接补发 `{"type":"title","title":"..."}`；
- 前端 [ChatView.vue processEvent](../../frontend/src/views/ChatView.vue#L465) 增加 `type === 'title'` 分支，复用现有 `updateCurrentSessionTitle` 并刷新会话列表；未知事件类型旧端自然忽略，协议向后兼容。

**C8. 去除 MiniLM 重复计算（P6）**

- [_calculate_relevance](../../backend/src/services/rag_chain.py#L313-L367) 改为直接使用检索分数判断相关性：有 `rerank_score` 时按新增 `KB_RELEVANCE_SCORE_THRESHOLD=0.3` 过滤，否则回退 dense/rrf 分位判断；删除问题+文档的 MiniLM 编码；
- semantic 策略新增 `STRATEGY_SEMANTIC_ENABLED=false`（默认关闭时该策略弃权，投票权重归一化到其余策略）；启用时改用共享 Ollama embeddings 计算相似度，移除 sentence-transformers 依赖路径。

**C9.（可选，P2）同请求 embedding 复用**

- 意图分类与 KB 检索的 query embedding 在单次请求内缓存复用，省一次 bge-m3 编码（约 100~300ms）。

---

## 5. 配置项变更清单（.env）

```ini
# --- 模型（必填，无内置默认；示例为当前环境取值）---
OLLAMA_MODEL_NAME=qwen3:4b
FAST_LLM_MODEL_NAME=qwen3:4b
EMBEDDING_MODEL_NAME=bge-m3:latest
EMBEDDING_DIMENSION=1024
OLLAMA_SUPPORTS_THINKING=true        # 换非思考模型（如 qwen2.5）时设 false
# KB_RERANK_MODEL=                   # 留空/不配置则关闭知识库 rerank，按 RRF 截断

# --- 本次新增开关 ---
KB_RERANK_ENABLED=true               # false 时按 RRF 分数截断，不加载 reranker
DECISION_VOTE_WHEN_KB_SELECTED=false # 显式选 KB 时跳过策略投票
CONTEXT_RESOLVE_USE_LLM=false        # 指代消解默认规则版，LLM 版仅兜底
STRATEGY_SEMANTIC_ENABLED=false      # 语义策略默认弃权（移除 MiniLM 依赖）
STRATEGY_LLM_TIMEOUT=5.0             # 策略投票 LLM 超时（秒）
INTENT_ROUTER_LLM_TIMEOUT=5.0        # 由 3.0 调整
KB_RELEVANCE_SCORE_THRESHOLD=0.3     #  rerank 分数相关性阈值
```

---

## 6. 文件改动清单

| 文件 | 改动项 |
|---|---|
| backend/src/config.py | A1 默认值中性化；C 组新增配置项 |
| backend/src/main.py | A2 启动模型校验 |
| backend/src/api/config.py | A3 响应默认值清理 |
| backend/src/services/model_manager.py | B1 `get_chat_llm` / B2 `get_embeddings` 工厂 |
| backend/src/services/rag_chain.py | B3 fast llm 注入；C5 投票开关；C8 相关性判断；注释清理 |
| backend/src/services/decision_pipeline.py | C5 选 KB 跳过投票 |
| backend/src/services/context_enhancer.py | B3/C3 规则前置 + fast llm |
| backend/src/services/strategies/llm_inference.py | B3 工厂 + C4 超时 |
| backend/src/services/strategies/semantic.py | C8 开关/停用 |
| backend/src/services/intent_router/llm_router.py | B3 think=False |
| backend/src/services/query_rewriter.py | B3 异步工厂 |
| backend/src/services/title_generator.py | B3 think=False |
| backend/src/services/milvus_service.py | B2 embeddings 单例 + C1 rerank 截断 |
| backend/src/services/hybrid_search.py | C1 截断参数透传（如需） |
| backend/src/services/web_search_service.py | B3 模型来源 + C1 核对 |
| backend/src/services/evaluation/generation_evaluator.py | B3 think=False |
| backend/src/services/evaluation/retrieval_evaluator.py | B2 embeddings 单例 |
| backend/src/api/chat.py | C7 标题异步 + title 事件 |
| frontend/src/views/ChatView.vue | C7 title 事件分支 |
| .env.example / .env.prod | A4 占位示例 |
| 对应 .pyi stub | 签名同步（llm_inference、rag_chain、model_manager 等） |

不新增业务代码文件（如工厂方法放入现有 model_manager.py）。

---

## 7. 实施顺序

1. **A 组**：配置去硬编码 + 启动校验（基础设施，先行）；
2. **B 组**：模型工厂与各服务切换（A 完成后即可验证）；
3. **C1 + C2**：rerank 截断 + 辅助任务关思考（收益最大，先落地）；
4. **C3~C8**：按 C3→C7→C5→C4→C6→C8 顺序逐项实施、逐项可测；
5. C9 视实测收益决定是否实施。

每组完成后运行对应测试与一次端到端问答，确认无回归再进入下一组。

---

## 8. 验证计划

1. **启动校验**：模型名留空/配错时启动失败并给出中文提示；正确配置正常启动（补充 test_startup_validation 用例）；
2. **单元/集成测试**：`test_intent_router*`、`test_rag_chain*`、`test_strategy*`、`test_stream*`、`test_hybrid_search*` 全绿；新增用例：rerank 候选截断数量、指代消解规则前置（无代词零调用）、选 KB 跳过投票、title 异步事件、辅助 llm 绑定 `reasoning=False`；
3. **延迟对比**：用 `backend/scripts/diag_latency.py` 与 Trace 页面，对比改动前后"选 KB + 第二轮追问"场景各阶段耗时（context_enhance / intent_route / kb_decision / kb_retrieval / generate），目标首字 ≤10 秒；
4. **前端实测**：SSE 流式正文、reasoning 面板、来源展示、title 后台更新、开/关深度思考两态，light/dark 模式浏览器验证；
5. `vue-tsc --noEmit` 与后端 ruff/类型检查通过。

---

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| rerank 截断影响检索质量 | 截断数取 `KB_HYBRID_RERANK_TOP_K=5`（与最终返回数一致，RRF 已保证候选质量）；`KB_RERANK_ENABLED=false` 可整体回退 |
| 规则版指代消解覆盖不足 | `CONTEXT_RESOLVE_USE_LLM=true` 可恢复 LLM 版；规则未命中代词时保留原问题，不产生错误改写 |
| 选 KB 跳过投票导致回答偏离文档 | 回答模板未变（仍要求优先基于参考信息、标注引用、信息不足时结合自身知识）；投票仍异步记录供学习引擎使用 |
| 标题异步更新失败 | 后台任务失败仅记日志，会话标题保持默认，下次问答仍会重试生成；end 事件不受影响 |
| 工厂改造影响面广 | 模型名解析仍走现有 fallback 链；逐服务切换并跑回归测试；.pyi stub 同步 |
| SSE 协议变更 | 仅新增 `title` 事件类型，旧客户端忽略未知字段即可 |

---

## 10. 实施记录（2026-09-07）

全部条目已实施，与方案差异点如下：

| 条目 | 实施说明 |
|---|---|
| A1-A4 / B1-B3 / C1-C7 | 按方案落地 |
| C2 深度思考两态 | `OLLAMA_DIRECT_MODEL_NAME` 深度思考关闭时的非思考专用模型，留空回退主模型绑定 `reasoning=False`（延续既有两模型架构约定） |
| C4 | `STRATEGY_LLM_TIMEOUT=5.0`，超时按弃权（置信度 0.3）处理 |
| C7 | 后端 `save_assistant_message` 仅保存消息并返回是否默认标题；end 先发，`generate_and_save_title` 后台任务生成标题落库后补发 `{"type":"title","session_id","title"}`。前端收到 end **不再主动 abort 连接**（否则收不到补发的 title），等流自然关闭；新增 `title` 事件分支复用 `updateCurrentSessionTitle`；空内容流以 `receivedEnd` 标记避免误报"连接断开"。异常中断路径不再生成标题（下次成功问答时补生成） |
| C8 相关性复算 | `_calculate_relevance` 改用检索分数：`KB_RERANK_ENABLED` + 模型已加载 + 文档带非零 `rerank_score` 时按 `KB_RELEVANCE_SCORE_THRESHOLD` 过滤；否则保守视为相关（rerank_score 退化为 RRF 排序分不可作相关性依据，避免误丢结果）。`KBReranker.model_loaded()` 为新增查询接口 |
| C8 语义策略 | `strategies/semantic.py` embedding 后端从 sentence-transformers MiniLM 换为 `model_manager.get_embeddings()` 共享单例（bge-m3），阈值沿用 0.75/0.65；默认关闭 |
| 未动项 | `sentence_transformer` 属性与 `_init_similarity_model()` 保留：kb_comparator / document_analyzer / knowledge_graph_generator 等低频功能仍按方案"本次不改"沿用 MiniLM 惰性加载；主问答链路不再触发加载。C9（同请求 embedding 复用）未实施，视实测收益决定 |
| 测试 | 后端全量 pytest 通过（test_semantic_cache_service TTL 用例存在与本改动无关的既有 flaky）；前端 vitest 40/40、vue-tsc 通过；test_strategy 断言更新为语义策略默认关闭（3 策略）并新增开关用例 |

---

## 11. 二次优化与 generate 瓶颈诊断（2026-10-03）

§10 落地后首字延迟仍偏高的反馈下，本轮先做"零功能损失提速"，随后用真实 trace 校正方向。

### 11.1 本轮已实施（第一批）

| 项 | 内容 | 位置 |
|---|---|---|
| A 冷启动预热 | lifespan 并发预热 `llm`、`llm_direct`（默认主力，此前从未预热）、bge-m3 embedding、CrossEncoder reranker、意图路由示例向量；GPU 组串行 / CPU 组并发 | [main.py](../../backend/src/main.py#L309-L350) |
| B 摘要后台化 | `_update_conversation_summary`（一次完整 LLM 调用）由 `await` 改 `create_task`，不再阻塞 `final` 事件 | [stages.py](../../backend/src/services/pipeline/stages.py#L679-L686) |
| C 相关性门控 | 优先复用已算好的 `rerank_score`（接上原死配置 `KB_RELEVANCE_SCORE_THRESHOLD`），reranker 未加载时才回退 MiniLM，且批量 encode 一次（原为 N+1 次串行） | [pipeline/retrieval.py](../../backend/src/services/pipeline/retrieval.py#L31-L106) |
| D 显存防抖 | embedding `num_gpu=0` 常驻 CPU；两处 CrossEncoder 与 MiniLM 显式 `device="cpu"`，不再与 LLM 抢 4GB 显存 | [model_manager.py](../../backend/src/services/model_manager.py#L266-L272) 等 |

### 11.2 本轮已实施（第二批）

| 项 | 内容 | 语义边界 |
|---|---|---|
| 精确匹配缓存前置 | 条目字段名改为规范化问题的 md5，命中退化为单次 Redis HGET；管线新增阶段 1.5，命中即回放短路 | 命中场景答案完全一致，零差异 |
| has_kb 跳过 LLM 冲突裁决 | 勾选知识库时规则冲突按规则优先级直接裁决，不再白付 5s 级 LLM 调用 | 极端冲突问题的路由结果会变 |
| 尾部校验后置 | 事实校验（完整 LLM 调用）从管线内联搬到 API 层：`end` 先发，后台校验 → 补发警告 → 落库（含警告） | 流式路径不再写 trace 的 `verification`；助手消息落库时点移到 `end` 之后（`shield`+任务集保证断连仍落库） |

验证：后端全量 pytest 916 passed / 83 skipped；前端 useChatStream 16 passed；镜像重建部署后启动日志 5 项预热全部完成，`ollama ps` 确认 bge-m3 100% CPU；意图路由 embedding 分类首问 6281ms → 61ms。

### 11.3 trace 实测：原第三批计划方向有误

拉取改动后 4 条真实 trace（`request_traces.stages`），结论与"第三批调上下文预算/多查询"的假设相反：

| primary_mode | total_ms | 耗时>100ms 的阶段 | completion tokens |
|---|---|---|---|
| agent_research | 8907 | generate=8845 | 207 |
| direct_llm | 13464 | intent_route=7421、generate=6042 | 148 |
| agent_research | 5885 | generate=5826 | 140 |
| agent_research | 9386 | intent_route=6281、generate=3102 | 68 |

两点关键事实：

1. **`generate` 占 60%~99%**（3.1~8.8s），而它的 `prompt_tokens` 仅约 180 —— prefill 极小。**`CONTEXT_TOKEN_BUDGET` / `max_history_turns` 与当前瓶颈无关**，第三批中的该项不应作为提速手段。慢在 **decode**（约 25~30 tok/s）。
2. **`intent_route`** 在 embedding 分类命中时仅 59~61ms（A 预热生效）；一旦未命中落到 LLM 路由，则耗时 6.3~7.4s 并仍降级 `direct_llm`（白付）。这与 §10 C6 把 `INTENT_ROUTER_LLM_TIMEOUT` 放宽到 5.0 有关：冷/慢模型下超时被吃满。

另注：默认知识库 `document_count=0`，**KB 侧优化（C 的 rerank 门控、精确缓存、语义缓存阈值）当前无法端到端验证**，需先导入文档。

### 11.4 generate 瓶颈根因（硬件实测）

环境：**NVIDIA GeForce RTX 3050 Laptop GPU，显存仅 4096 MiB**；宿主 Ollama 环境 `OLLAMA_FLASH_ATTENTION=1`、`OLLAMA_KV_CACHE_TYPE=q8_0`、`OLLAMA_KEEP_ALIVE=30m`、**`OLLAMA_NUM_PARALLEL=2`**。

直答模型 `qwen3:4b-instruct-2507-q4_K_M`（权重约 2.5GB）在工作集 3.9GB 时只能 **60% 上 GPU（40% CPU）**，权重部分卸载即为 decode 慢的根因。`num_ctx` 对照实测（同一 prompt）：

| num_ctx | CPU/GPU | 工作集 | decode |
|---|---|---|---|
| **6144（现网）** | 40%/60% | 3.9 GB | **27.6 tok/s** |
| 4096 | 33%/67% | 3.6 GB | 31.5 tok/s |
| 3072 | 31%/69% | 3.4 GB | 33.0 tok/s |
| 2048 | 27%/73% | 3.2 GB | 35.0 tok/s |
| 1024 | 23%/77% | 3.1 GB | 39.9 tok/s |

即：KV cache 越大 → 留给权重的显存越少 → 卸载到 CPU 的层越多 → decode 越慢。KV 占用同时受 `OLLAMA_NUM_PARALLEL=2`（双 slot，KV 近似翻倍）影响，该值无法在应用侧覆盖，需改宿主 Ollama 环境后重启。

### 11.5 推荐模型（待定，需自行选型）

**架构约束**：`FAST_LLM_MODEL_NAME` 在 `think=False` 时会被重定向到 `OLLAMA_DIRECT_MODEL_NAME`（[model_manager.py](../../backend/src/services/model_manager.py#L229-L234)），因此**直答与全部辅助任务（意图路由/标题生成/Query 改写）当前共用同一个 4B 模型**。若只把辅助任务换小模型、直答仍留 4B，同一请求内会 1.7B↔4B 反复换入换出——正是 D 要消除的显存抖动。**结论：小模型应接管默认路径（deep_thinking=off）的直答 + 辅助，4B 仅保留给显式 deep_thinking=on。**

候选（本地当前均未安装，需 `ollama pull`；磁盘余量 154 GB 充足）：

| 候选 | 体积 | 预期 | 取舍 |
|---|---|---|---|
| `qwen2.5:3b-instruct` | ~1.9 GB | 预计可基本全量上 GPU，decode 明显提升 | **推荐**：中文与指令遵循（引用标注/禁前缀等）较可靠，质量损失最小 |
| `qwen2.5:1.5b-instruct` | ~1 GB | 全量上 GPU，预计 60+ tok/s | 速度提升最大，但中文指令遵循明显变弱，可能破坏引用标注等规则 |
| `qwen3:1.7b` | ~1.4 GB | 全量上 GPU | qwen3 默认输出思考链，而 `llm_direct` 定位是"从不生成思考块"的非思考模型，需额外适配，架构上不干净 |

落地方式（选定后）：`.env.prod` 设 `FAST_LLM_MODEL_NAME` 与 `OLLAMA_DIRECT_MODEL_NAME` 为该模型；`OLLAMA_MODEL_NAME`（deep_thinking=on）保持 4B；`OLLAMA_SUPPORTS_THINKING` 需按所选模型区分能力（qwen2.5 无思考，若主模型仍为 qwen3 则保持 `true`，此时 1.7B 类思考小模型不适配）。更换 embedding 会改变向量口径、须重建 Milvus 索引，**本次不涉及 embedding**。

### 11.6 未采纳/待定的其他杠杆

| 杠杆 | 预期 | 未采纳原因 |
|---|---|---|
| `OLLAMA_NUM_PARALLEL` 2→1 | 释放 KV 显存、在**不压缩上下文窗口**前提下提高 GPU 卸载 | 需重启宿主 Ollama；并发 LLM 调用串行化（与 B 的后台摘要可能让下一问等待） |
| `OLLAMA_NUM_CTX` 6144→4096 | 配合上项释放更多显存 | 会压缩 KB 上下文裕度（`CONTEXT_TOKEN_BUDGET=4000` 接近上限），需与 budget 联动并 A/B |
| 降低 `INTENT_ROUTER_LLM_TIMEOUT` | 消除 5~7.4s 白等 | 会改变部分问题的路由结果，需单独决策 |

### 11.7 结论

- 本轮"零功能损失"批次已全部落地并验证，A（意图示例预热）与 D 已产生可测收益。
- **当前剩余延迟的主要来源是 generate 段的权重部分卸载**，受限于 4GB 显存；上下文预算类旋钮对该瓶颈无效。
- 下一步待用户选定 §11.5 的小模型，再做 A/B 与配置切换；选定前不再改动模型配置。

---

## 12. 直答模型 A/B 实测结果（2026-10-03）

按 [direct-model-switch-plan-2026-10-03.md](../archive/direct-model-switch-plan-2026-10-03.md) 执行 A/B；完整执行记录（含偏差、方法学、遗留）见该文档 §6，此处只记结论。

候选：A=`qwen3:1.7b`、B=`qwen2.5:3b-instruct`（均 100% GPU）。基线 `qwen3:4b-instruct-2507-q4_K_M` 在执行前已被删除，基线列采用 §11.4 同机同 ctx 实测数据。

| 维度 | 基线 4B-instruct | 候选 A 1.7b | 候选 B 3b-instruct |
|---|---|---|---|
| GPU 分层 | 40%/60% | **100% GPU** | **100% GPU** |
| decode（trace 换算） | 27.6 tok/s | 90~107 tok/s（含隐藏思考） | 55~71 tok/s（无思考） |
| 短答 generate（墙钟） | 3.1~8.8 s | 2.0~5.0 s | **0.9~1.6 s** |
| KB 引用标注 | — | Q4/Q6/Q9 均 `[1]` ✓ | Q4 ✓，Q6 稳定漏 `[1]` |
| 思考链泄漏 | 无 | 无（但每次多生成隐藏思考） | 无 |

要点：
- **两个候选均消除权重 CPU 卸载**（`ollama ps` 100% GPU），decode 均远超 45 tok/s 目标。
- **候选 A 的 tok/s 数字被隐藏思考 token 抬高**：同一 KB 短答 A 记 179 tokens、B 仅 26，故 A 墙钟反而更慢；A 是思考模型却位于"非思考直答"槽位，架构不契合。
- **候选 B 是原生非思考模型**（契合 `llm_direct` 定义），墙钟最快；代价是 Q6 稳定漏 `[1]`（达门槛上限）。
- **已定稿候选 B（2026-10-03）**：按"均合格取速度更快者"规则取墙钟最快者；A 的 tok/s 优势为隐藏思考 token 假象（同题 179 vs 26 token），且思考模型占用非思考槽位架构不契合。`README.md` / `docs/architecture.md` 模型表已同步。Q6 漏 `[1]` 已通过 KB 回答模板强化引用要求修复（实测 B 的 Q6 3/3 带引用），落选候选 A 已按指示删除。
- 期间发现两个**与模型无关**的既有问题：①天气城市提取器对含"温度"的问题误判（`_extract_city_name`）；②KB `kb_retrieval` 阶段耗时偏高，瓶颈为 CPU 上的 CrossEncoder 重排（bge-m3 embedding 仅 ~45 ms）。经 2026-10-08 `KB_HYBRID_RERANK_TOP_K` 5→3 后，稳态由 2.8~3.1 s 降至 **1.3~1.8 s**（重启后首查另含 Milvus 首次加载约 4~5 s 的一次性成本，与该参数无关）；实测拆解与冷启动口径详见 [direct-model-switch-plan-2026-10-03.md §6.4](../archive/direct-model-switch-plan-2026-10-03.md)；该指标后由 §13 的 reranker 换型进一步降至 **441~560 ms**。

---

## 13. 后续优化实测（2026-10-12）

§11/§12 遗留项先后落地，结论备查：

1. **多轮会话第 3+ 问 generate 41~75 s 慢例**：根因＝`ContextEnhancer(self.llm)` 注入 4B 主模型（§4-B3 要求 fast llm，漏改），每轮后台摘要/指代消解调用 4B → Ollama 驱逐 3B 直答模型并加载 4B（3.9 GiB 挤爆 4 GB 显存）形成换载拉锯（宿主 `server.log` evicting + qwen3:4b 加载时序实证）。修复为 `ContextEnhancer(fast_llm)`，post-fix Q3 generate **41068 ms → 2490 ms**。同批修复：lifespan 去掉 4B 预热（4B 驻留会驱逐 3B；4 GB 显存机器 deep_thinking=on 首问改为按需加载 10~30 s，≥8 GB 机器可恢复预热）。
2. **reranker 换型**：`KB_RERANK_MODEL` / `SEARCH_RERANK_MODEL` → `bge-reranker-base`（离线 A/B 10 题 × 3 配置质量零差异、推理快 3.3 倍；base 权重已入容器 HF 卷，v2-m3 保留可回滚）。实测 kb_retrieval 热身 1.3~1.8 s → **441~560 ms**，KB 问答整链 wall 0.9~1.5 s，引用质量验证通过（USB×2 / CCC+CE 排序引用正确）。语料显著增长后需复评。
3. **既有问题实锤收口（2026-10-13）**：①~~精确缓存 TTL 过长~~——**伪问题**：Redis 实测全库仅 1 key / 17 条目、age 19~21h（<24h TTL）、key 层 redis_ttl 5.7h，此前"5 天仍命中"系时间线归因错误（第三轮 bench 命中的是数小时前同日写入的条目，非 10-08）；TTL 机制健康，未改动。②~~`AnswerVerifier(self.llm)` 4B 注入~~——**已修复**：web 答案后台校验改注入 `fast_llm`（与 ContextEnhancer 同机制，避免校验拉起 4B 驱逐 3B），容器内实测 verifier/enhancer 均为 `qwen2.5:3b-instruct`。至此应用侧全部 LLM 消费点归位：4B 仅剩 `deep_thinking=on` 主回答链。
4. **天气城市提取器误判已修复（2026-10-12）**：§12 ① 的"工作温度范围"误路由。三层防线：`_TECH_TEMPERATURE_RE` 排除技术复合温度词（工作/CPU/存储温度等，裸"温度"不加守卫避免误伤真实问法）+ `_is_place_name` 地名校验（jieba.posseg ns 词性 + 内置常见城市白名单，兜住"吕梁"被错标 nr 的场景）+ 非地名残片继续尝试后续模式。端到端验证：技术温度问走 KB 返回 `-20℃至60℃ [1]` ✓，北京/吕梁真实天气正常触发工具 ✓。注意：裸"温度/气温"不加入技术排除表（"上海温度范围"是真问法），由地名校验兜底。

