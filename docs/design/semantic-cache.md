# P1-3 语义缓存设计

> 状态：已实施（2026-09-07），实施记录见文末 §8
> 关联：roadmap P1-3（相似问题命中缓存，降本提速）

## 1. 背景与现状

**现状盘点**（探索结论，修正 roadmap 中「cache_service.py 为精确匹配缓存」的表述）：

| 现有设施 | 位置 | 说明 |
|---|---|---|
| Redis 通用缓存 | `src/services/cache_service.py` | 统一 Redis 封装（get/set/delete/clear_pattern），fail-open，仅被联网搜索缓存与知识库列表缓存使用，**没有问答答案缓存** |
| 快捷问题建议缓存 | `src/api/session.py` | 进程内 LRU（256 条），缓存的是「追问建议」不是答案 |
| 问答主链路 | `rag_chain._pipeline()` | 阶段：上下文增强 → 时间类快捷返回 → 意图路由 → tool_first → KB 决策 → Agent → 联网搜索 → KB 检索 → 生成 |
| Embedding 能力 | `OllamaEmbeddings(bge-m3)` + `aembed_query()` | 意图分类器（`embedding_classifier.py`）、Milvus 检索均已使用，模式成熟 |
| 时效性判断 | 意图路由 `needs_realtime` + 阶段 2 时间类快捷返回 | 时间/日期问题不进入 KB 检索 |

**痛点**：同一/相似问题重复提问时，完整走「检索 → 重排序 → LLM 生成」全链路（2~10s + token 消耗），无任何答案级复用。

## 2. 目标与验收标准

**目标**：纯知识库问答场景下，相似问题直接返回缓存答案，省去检索+重排+生成；命中响应 < 500ms（不含 SSE 建连）。

**验收标准**：
1. 相同问题（精确匹配，忽略大小写/首尾空白）二次提问命中缓存，不触发 KB 检索与 LLM 调用。
2. 语义相似问题（cosine ≥ 阈值 0.92）命中缓存，SSE 流中带「缓存命中（相似问题）」reasoning 步骤，前端推理时间线可见，无前端改动即正常渲染。
3. 工具类/时效性/联网搜索/深度思考开启的问题**不读不写**缓存（单测覆盖各排除分支）。
4. Redis 不可用时缓存读写静默降级，主流程不受影响（fail-open，单测覆盖）。
5. 文档上传/删除/重建后，该知识库相关缓存条目失效（单测覆盖失效匹配逻辑）。
6. Prometheus 指标：`semantic_cache_hits_total` / `semantic_cache_misses_total` / `semantic_cache_stores_total` 可查。
7. 新增单测全绿，现有测试不回归。

## 3. 技术方案

### 3.1 总体思路

在 `rag_chain._pipeline()` 统一管线内插桩（流式/非流式共用，一处改动双端生效）：

```
阶段 3 意图路由 → 阶段 4 工具 → 阶段 5 KB决策
                                     │
                              [新增] 语义缓存查找（仅当判定会走纯KB问答路径）
                                     │ 命中 → 发 reasoning 步骤 + 流式回放缓存答案 + _finalize
                                     ▼
阶段 6 Agent → 阶段 7 联网搜索 → 阶段 8 KB检索 → 阶段 9 生成
                                     │
                              [新增] 答案落库（finalize 后异步后台写入，仅纯KB答案）
```

### 3.2 缓存键与命中判定

**缓存范围（scope）**——按「会改变答案的因素」划分，key 形如：

```
rag:semcache:{user_id}:{kb_scope}
# kb_scope = "all"（未选库）或 md5(sorted(kb_ids) join ",")[:12]
```

- `user_id` 入 scope：本项目知识库严格按用户隔离，跨用户命中必然泄露他人文档内容，**明确不做跨用户缓存**（收益主要来自同一用户重复/追问场景）。
- `deep_thinking`：仅 off 模式参与（读写均排除 on）。on 模式走混合思考模型 + reasoning 流，回放语义不同，v1 不做。
- `use_web_search=True`：不参与（联网结果有时效性）。

**条目结构**（Redis HASH，每 scope 一个 key，field 为 uuid4）：

```json
{
  "question": "改写后的自包含问题（resolved_question）",
  "question_norm": "归一化问题（lower + strip，用于精确匹配快路径）",
  "embedding": [1024 维 float],
  "answer": "完整答案文本",
  "source_texts": ["..."],
  "source_metadata": [{...}],
  "kb_ids": ["命中的知识库范围", "all 表示全域查询"],
  "answer_type": "knowledge_base",
  "created_at": 1736000000.0
}
```

**查找算法**（`semantic_cache_service.lookup`）：
1. `HGETALL` scope hash → 逐条惰性过滤过期条目（TTL 检查用 `created_at`，读取时不删、写入时顺手清理）。
2. **精确匹配快路径**：`question_norm` 相等 → 直接命中（相似度记 1.0）。
3. **语义匹配**：对当前 `resolved_question` 调 `aembed_query()`，与条目 embedding 算余弦相似度，取最高分且 ≥ 阈值（默认 0.92）的条目。
4. 单 scope 条目数上限（默认 200）：查找时只比较最近 N 条（按 created_at 排序），写入超限时 HDEL 最旧条目。

**关键决策——以 `resolved_question` 为缓存键**：阶段 1 已完成指代消解，`resolved_question` 是自包含问题。多轮追问「它多少钱？」会被改写成「XX手机多少钱？」再参与匹配，因此跨会话、跨历史命中是安全的。生成 prompt 中虽含 history_context，但 KB 答案主要源自检索上下文 + 自包含问题，残余风险见 §6。

### 3.3 写入条件（仅纯知识库问答）

`_pipeline` finalize 前判断，全部满足才异步写入（`asyncio.create_task`，不阻塞响应）：

| 条件 | 依据 |
|---|---|
| `state.answer_type == "knowledge_base"` | 真实 KB 命中且带来源的答案（排除 llm_direct / web / hybrid / tool / agent） |
| `not state.web_sources_for_citation` | 无联网来源（时效性） |
| `state.decision.mode in (PURE_KB, HYBRID_INTELLIGENT)` | 排除 FUNCTION_CALLING / AGENT_SEARCH / WEB_SEARCH / HYBRID_SEARCH（工具/联网路径） |
| `state.deep_thinking != "on"` | off 模式答案确定性高 |
| `state.docs` 非空 | 无来源的答案不缓存 |
| `intent_decision.needs_realtime != True` | 沿用现有时效性判断 |
| Redis 可用 | fail-open |

### 3.4 失效策略

1. **TTL**：每条目 24h（可配 `SEMANTIC_CACHE_TTL_HOURS`），读取时惰性过滤。
2. **容量淘汰**：单 scope 超 200 条（可配）删最旧。
3. **知识库内容变更失效**（主动）：文档上传处理完成 / 文档删除 / 文档重建 / 知识库删除时，调用 `semantic_cache_service.invalidate_kb(kb_id)`：
   - `SCAN rag:semcache:*`（低频操作，条目量小，可接受）；
   - 对每个 scope hash 逐 field 解析 `kb_ids`，命中 `kb_id ∈ entry.kb_ids` **或** `entry.kb_ids == ["all"]` 的条目 HDEL（全域查询可能引用了该库）。
   - 挂载点：`document.py` 的 `process_document_async`（成功/失败都失效，保守处理）与 `process_document_delete_async`、`/{doc_id}/reprocess`；`knowledge_base.py` 的删除接口。

### 3.5 命中后的 SSE 回放

保持 SSE 协议不变，前端零改动：

1. 发一条 reasoning 步骤（新增 `REASONING_STEP_CACHE_HIT = "cache_hit"` 常量，加入 `reasoning.py` 步骤常量区）：
   `{type: "reasoning", step: "cache_hit", status: "done", title: "缓存命中", content: "命中相似问题（相似度 0.95），直接返回缓存答案", duration_ms: <lookup耗时>}`
2. 将 `entry.answer` 按 ~120 字符切片，逐片以 `("chunk", part, source_texts, source_metadata, "knowledge_base")` 产出（与正常流式事件结构一致；`answer_type` 保持 `knowledge_base`，前端来源卡片/end 事件逻辑不变）。
3. `state.final_answer / source_texts / source_metadata / answer_type` 赋值后 `state.finished = True` → `_finalize()` 照常落 trace、更新会话摘要。
4. Trace 增加 `trace.add_stage("semantic_cache", "hit", duration_ms)` 与 `trace.data["semantic_cache_hit"] = {"score": ..., "question": 缓存原始问题}`，Trace 查询页可辨识缓存命中（Trace UI 对未知 stage 的展示为通用标签，无需改动）。
5. 缓存命中路径不调用 `_record_token_usage`（未消耗 LLM token），trace 中该字段留空即代表零消耗。

### 3.6 新增配置（`config.py` 新增 `SemanticCacheSettings`，风格对齐既有嵌套 Settings）

```python
class SemanticCacheSettings(BaseSettings):
    SEMANTIC_CACHE_ENABLED: bool = True            # 总开关（false 时查找/写入全部跳过）
    SEMANTIC_CACHE_SIMILARITY_THRESHOLD: float = 0.92
    SEMANTIC_CACHE_TTL_HOURS: int = 24
    SEMANTIC_CACHE_MAX_ENTRIES: int = 200          # 单 scope 条目上限
    SEMANTIC_CACHE_EMBED_BATCH_CHARS: int = 120    # 命中回放切片长度
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")
```

`.env` / `.env.dev` 同步补注释示例（全部有默认值，不配也能跑）。

### 3.7 新增监控（`middleware/prometheus.py`，风格对齐 KB_QUERIES）

```python
SEMANTIC_CACHE_HITS = Counter("semantic_cache_hits", "语义缓存命中次数")
SEMANTIC_CACHE_MISSES = Counter("semantic_cache_misses", "语义缓存未命中次数（含禁用场景不计）")
SEMANTIC_CACHE_STORES = Counter("semantic_cache_stores", "语义缓存写入次数")
SEMANTIC_CACHE_LOOKUP_LATENCY = Histogram("semantic_cache_lookup_seconds", "语义缓存查找耗时（含 embedding）")
```

### 3.8 Embedding 复用

新建的 `semantic_cache_service.py` 内部按 `EmbeddingIntentClassifier._get_embeddings()` 同款懒加载模式持有 `OllamaEmbeddings(model=settings.model.EMBEDDING_MODEL_NAME)`，失败静默降级（返回 miss，不阻塞主流程）。不与意图分类器共享实例（各自独立生命周期，避免单测相互污染），模型一致因此无额外显存/内存开销（Ollama 服务端单例）。

## 4. 改动清单（精确到文件）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src/services/semantic_cache_service.py` | **新增**。`SemanticCacheService`（普通类，非单例——无状态，方法均传入 scope）：`lookup(user_id, kb_ids, question, embedding_fn)`、`store(entry)`、`invalidate_kb(kb_id)`；内部懒加载 OllamaEmbeddings + cosine 计算 + 容量/TTL 管理；全部 Redis 操作经 `CacheService`，异常 fail-open 返回 miss/False |
| 2 | `src/services/reasoning.py` | 新增 `REASONING_STEP_CACHE_HIT = "cache_hit"` 常量 |
| 3 | `src/services/rag_chain.py` | `_pipeline` 阶段 5 后插入缓存查找短路分支（~40 行）；`_finalize` 前插入后台写入判断（~30 行）；`_PipelineState` 增加 `semantic_cache` 字段记录命中信息 |
| 4 | `src/config.py` | 新增 `SemanticCacheSettings` + `Settings` 挂载 `semantic_cache` 属性 |
| 5 | `src/middleware/prometheus.py` | 新增 3 Counter + 1 Histogram + `record_semantic_cache_*` 辅助函数 |
| 6 | `src/api/document.py` | `process_document_async` 成功/失败路径、`process_document_delete_async`、`reprocess_document` 调用 `invalidate_kb`（fire-and-forget，带超时保护，失败仅告警） |
| 7 | `src/api/knowledge_base.py` | 删除知识库接口调用 `invalidate_kb` |
| 8 | `.env` / `.env.dev` | 补 `SEMANTIC_CACHE_*` 注释示例（默认值即可运行） |
| 9 | `docker-compose.yml` / `docker-compose.dev.yml` | backend environment 补 `SEMANTIC_CACHE_ENABLED` 等变量（遵循「compose 必须显式传环境变量」约束） |
| 10 | `tests/test_semantic_cache_service.py` | **新增**。单测：精确命中 / 语义命中≥阈值 / 相似但低于阈值 miss / TTL 过期 / 容量淘汰最旧 / invalidate_kb 匹配（含 all 通配）/ Redis 失败 fail-open / cosine 边界（零向量） |
| 11 | `tests/test_semantic_cache_pipeline.py` | **新增**。管线级单测（mock rag_chain 依赖）：命中短路（不触发检索与生成，SSE 含 cache_hit reasoning + content 回放）/ 工具与联网与深度思考 on 不读写 / finalize 后符合条件的答案异步落库 |

**不改动**：前端（SSE 协议不变，reasoning 步骤为通用渲染）；chat.py（管线内插桩，API 层无感知）；Milvus（不用向量库存缓存向量，Redis JSON 足够）。

## 5. 性能与成本预估

- **命中路径**：1 次 `aembed_query`（bge-m3 via Ollama，~50-150ms）+ HGETALL + 内存余弦（200 条 × 1024 维 < 5ms）→ 总计 < 300ms，对比全链路 2~10s。
- **未命中路径**：额外支出 = 1 次 embedding（~100ms）+ 写入异步后台。意图分类器本身已做过一次问题 embedding，两者同模型；Ollama 服务端对相同输入有 KV 复用但接口层不共享，100ms 属可接受开销。
- **Redis 内存**：单条 ~15-25KB（1024 维 JSON + 答案 + 来源），200 条/scope ≈ 5MB，多用户场景按 scope 线性增长，Redis 容量内可控。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 相似但不等价的问题返回错误答案（如「A产品的价格」vs「B产品的价格」实体不同但句式相似，bge-m3 可能 > 0.92） | 阈值取高（0.92）；仅 KB 域（答案有据可查，用户可从来源卡片发现不符）；TTL 24h；命中时 reasoning 时间线明示「来自相似问题」，用户可感知并追问纠正；后续可加「来源重合度」二次校验（本期不做，观察误命中反馈再定） |
| 多轮对话 history_context 影响生成语气/指代，缓存答案与当前上下文风格不符 | resolved_question 已自包含指代；KB 答案以检索内容为主。残余风险接受，跟踪 badcase 反馈 |
| 用户文档刚更新但缓存未失效 | 上传/删除/重建/删库四个入口全部挂失效钩子（含处理失败路径，保守失效）；TTL 兜底 |
| Redis 中 JSON 存 1024 维向量体积偏大 | 200 条上限 + TTL 双重控制；未来量大可改 float16 base64 编码（预计 ~2.7KB/条），本期不做 |
| 缓存写入与主响应竞争事件循环 | 写入为 `asyncio.create_task` 后台执行，且有 `asyncio.wait_for` 超时保护 |
| 误命中难以发现 | Trace 落 `semantic_cache_hit` 数据（score + 原问题），可按用户查 Trace 排查；Prometheus 命中率可观测 |

## 7. 明确不做（本期范围外）

- 跨用户缓存共享（知识库隔离模型下有泄露风险，收益存疑）。
- deep_thinking=on 的答案缓存（reasoning 流回放语义复杂，off 模式覆盖高频场景）。
- llm_direct（无知识库闲聊）答案缓存——通用开放域对话误命中代价高且收益低（生成快），与 roadmap「仅纯知识库问答」一致。
- Milvus 独立缓存集合 / 近似最近邻检索（条目规模小，暴力余弦足够）。
- 缓存命中答案的「来源重合度」二次校验、命中率管理后台 UI。

## 8. 实施记录（2026-09-07）

- 与设计稿的差异：
  - 回放切片配置项命名调整为 `SEMANTIC_CACHE_REPLAY_CHUNK_CHARS`（设计稿中的 `SEMANTIC_CACHE_EMBED_BATCH_CHARS` 语义不清），默认值 120 不变。
  - `lookup` 返回 `(SemanticCacheHit | None, embedding | None)` 元组：未命中时回传本次 embedding，`store` 复用后可省一次 `aembed_query`（§3.8 Embedding 复用的落地形式）。
  - `store` 采用显式参数签名（`user_id, kb_ids, question, answer, source_texts, source_metadata, embedding=None`）而非 entry 对象。
  - 新增模块级 `schedule_invalidation(kb_id)` 辅助函数（fire-and-forget + 超时保护），document.py / knowledge_base.py 失效钩子统一复用。
- 落地文件与设计稿 §4 清单一致；`_PipelineState` 新增 `semantic_cache_hit` / `semantic_cache_embedding` 字段。
- 测试：`test_semantic_cache_service.py` + `test_semantic_cache_pipeline.py` 共 45 个用例（命中回放短路、各排除分支、TTL/容量淘汰、失效匹配含 all 通配、fail-open、cosine 边界）；全套 **688 passed / 100 skipped** 无回归。
- 顺带修正 `test_sql_echo_and_logging.py::test_default_is_off`：默认值断言改为隔离环境（`delenv` + `_env_file=None`），不再受根目录 `.env` 中开发期 `SQL_ECHO=True` 干扰。
