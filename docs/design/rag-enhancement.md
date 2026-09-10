# RAG 知识库系统升级设计方案

> 状态：部分实施（混合检索/重排序/评估闭环/意图路由已落地；分阶段进度并入 [improvement-roadmap.md](improvement-roadmap.md) 推进）

## 1. 背景与目标

### 1.1 当前状态

项目已具备相对完整的 RAG 基础能力：

- **向量检索**：基于 Milvus + `bge-m3:latest` 的语义检索（1024 维稠密向量）。
- **联网搜索**：SearXNG + 查询改写 + 结果重排序 + 引用补全/答案校验。
- **Agent 模式**：Function Calling / ReAct Agent 用于复杂问题。
- **工具调用**：天气、时间、金价、汇率、计算器等垂直工具。
- **意图路由**：基于规则的关键词匹配路由。
- **输出治理**：引用补全、事实校验、输出污染清洗、链路追踪持久化。

### 1.2 主要差距

对照工业级 RAG 最佳实践，当前系统在**知识库检索侧**仍有明显短板：

| 维度 | 当前实现 | 标准实践 | 影响 |
|------|---------|---------|------|
| 检索方式 | 仅向量检索（IP/HNSW） | 向量 + 关键词（BM25/全文）混合检索 | 缩写、专有名词、ID 召回不足 |
| 结果精排 | Web 搜索有重排序，知识库无 | 统一 Cross-Encoder 重排序 | 顶部结果相关性差 |
| 文档分块 | 固定 RecursiveCharacterTextSplitter | 语义/结构/多粒度分块 | 边界切割语义、表格/代码丢失结构 |
| 上下文构建 | 简单拼接 | 压缩 + 重排序 + 去噪 | 长上下文 Lost in the Middle、噪声多 |
| 意图路由 | 规则 + 关键词 | 规则 + Embedding + 轻量分类模型 | 新场景扩展困难、误判 |
| 模型分工 | 单一 LLM 承担所有任务 | 专用小模型处理路由/抽取/重写/判断 | 主模型负担重、延迟高、成本高 |
| 评估体系 | 少量端到端测试 | 检索指标 + 生成指标 + LLM-as-Judge | 无法量化优化效果 |
| 可观测性 | Prometheus 指标 + TraceCollector | 检索/生成全链路trace + badcase闭环 | 问题定位依赖日志 |

### 1.3 设计目标

在不引入付费 API、保持离线开源的前提下，将知识库检索侧能力从“能用”提升到“可量化、可优化、可扩展”：

1. **检索召回率提升**：引入关键词通道，与向量检索做 late fusion。
2. **顶部精度提升**：知识库结果统一经 Cross-Encoder 重排序。
3. **文档理解增强**：支持结构感知分块与语义分块。
4. **上下文质量提升**：上下文压缩、重排序、动态截断。
5. **路由智能化**：在保持规则兜底的同时，增加 embedding-based 与在线学习路由。
6. **模型解耦**：引入轻量专项模型/策略，分担主 LLM 工作。
7. **评估闭环**：建立检索/生成评测集与指标看板。
8. **可观测增强**：检索步骤全链路 trace 与 badcase 标记。

---

## 2. 总体架构

```text
┌─────────────────────────────────────────────────────────────────┐
│                         User Query                              │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  Intent Router（意图路由）                                       │
│  - 规则兜底（问候/天气/计算/汇率/金价）                          │
│  - Embedding-based 分类（KB/Web/Direct/Agent）                   │
│  - 在线学习层（rule_learner 扩展）                               │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  Query Rewriting（查询改写）                                     │
│  - 指代消解、上下文补全、多查询扩展                              │
│  - 轻量模型/规则混合                                             │
└──────────────────────┬──────────────────────────────────────────┘
                       │
         ┌─────────────┼─────────────┐
         │             │             │
         ▼             ▼             ▼
   ┌─────────┐   ┌─────────┐   ┌──────────┐
   │ KB Dense│   │ KB BM25│   │ Web Search│
   │ Search  │   │ Search │   │ (已有)    │
   └────┬────┘   └────┬────┘   └────┬─────┘
        │             │             │
        └──────┬──────┘             │
               │                    │
               ▼                    ▼
        ┌─────────────────┐  ┌──────────────┐
        │ Fusion & Rerank │  │ Web Result   │
        │ (Cross-Encoder) │  │ Postprocess  │
        └────────┬────────┘  └──────┬───────┘
                 │                  │
                 └────────┬─────────┘
                          ▼
        ┌─────────────────────────────────────┐
        │   Context Builder                   │
        │   - 压缩（LLM/语义）                 │
        │   - 重排序（Lost in the Middle）     │
        │   - 去重、溯源编号                   │
        └──────────────────┬──────────────────┘
                           │
                           ▼
        ┌─────────────────────────────────────┐
        │   LLM Answer Generator              │
        │   - 专用 prompt（KB/Web/Tool）       │
        │   - Citation backfill / Verify      │
        └──────────────────┬──────────────────┘
                           │
                           ▼
        ┌─────────────────────────────────────┐
        │   Evaluation & Trace                │
        │   - 指标：Hit Rate / MRR / NDCG      │
        │   - LLM-as-Judge                     │
        │   - Prometheus / TraceCollector      │
        └─────────────────────────────────────┘
```

---

## 3. 分模块详细方案

### 3.1 检索层：混合检索 + 重排序

#### 3.1.1 问题

- `MilvusService.search()` 仅使用 IP 度量 + HNSW 向量索引，对以下查询召回不足：
  - 缩写、ID、型号、代码片段、人名、法律条款编号。
  - 与训练数据分布差异大的专业术语。
- 没有利用已持久化的 `content` 字段做全文检索。

#### 3.1.2 方案

**A. 引入 BM25/全文检索通道**

由于项目强制使用 Milvus，优先使用 Milvus 自身的 **Sparse Vector / BM25 索引**（Milvus 2.4+ 支持 `sparse_vector` + `BM25`），避免引入额外服务（如 Elasticsearch）。

- 新增字段：`sparse_embedding`（类型 `SPARSE_FLOAT_VECTOR`）。
- 使用 Milvus 的 `BM25` 函数从 `content` 字段生成稀疏向量。
- 查询时同时执行：
  - `dense search`（已有 embedding）。
  - `sparse search`（BM25）。

**B. Late Fusion 融合排序**

对两个通道的结果使用 **RRF（Reciprocal Rank Fusion）**：

```text
score = Σ 1 / (k + rank_i)
```

- `k` 默认 60，可调。
- 支持按 `kb_id` / `document_id` 过滤。
- 结果去重：同一 `document_id + chunk_index` 合并取最高融合分。

**C. Cross-Encoder 重排序**

- 复用 `qllama/bge-reranker-v2-m3:latest`（通过 `OllamaReranker` 本地调用，同时支持 `sentence_transformers` 加载方式切换）。
- 对 Fusion 后的 Top-K（如 20/50）做精排，最终取 Top-N 给 LLM。
- 增加重排序置信度阈值，低于阈值的结果标记为“可能不相关”，供后续上下文构建过滤或提示模型谨慎使用。

**D. 索引参数优化**

- HNSW 参数当前为 `M=8, efConstruction=64`，偏小。建议：
  - `M=16`, `efConstruction=128`（建立索引时）。
  - 检索时 `ef=128`（当前 64）。
- 新增配置项：
  - `MILVUS_EF_CONSTRUCTION`
  - `MILVUS_EF`
  - `MILVUS_HNSW_M`
  - `KB_HYBRID_SEARCH_TOP_K`
  - `KB_HYBRID_RERANK_TOP_K`
  - `KB_RRF_K`

#### 3.1.3 数据流

```python
# 伪代码
async def search_hybrid(query, kb_ids=None, document_ids=None):
    dense_results = await milvus_service.search_dense(query, k=top_k, ...)
    sparse_results = await milvus_service.search_sparse(query, k=top_k, ...)
    fused = reciprocal_rank_fusion(dense_results, sparse_results, k=rrf_k)
    reranked = await reranker.rerank(query, fused, top_n=rerank_top_k)
    return reranked
```

#### 3.1.4 兼容性

- 新集合自动创建 `sparse_embedding` 字段；旧集合通过迁移脚本 `backend/scripts/migrate_hybrid_index.py` 添加字段并重建索引（需重新跑一遍文档生成 sparse vector）。
- 配置开关 `KB_ENABLE_HYBRID_SEARCH`（默认 True），可关闭退回纯向量检索。

---

### 3.2 文档处理层：智能分块

#### 3.2.1 问题

- 当前 `split_documents()` 使用固定 `RecursiveCharacterTextSplitter`，对所有文件类型统一参数。
- 表格、代码、Markdown 标题结构被破坏。
- 未根据内容类型选择分块策略。

#### 3.2.2 方案

引入**分块策略工厂**，根据文档类型和结构选择策略：

| 文档类型 | 策略 | 说明 |
|---------|------|------|
| 普通文本/TXT | RecursiveCharacterTextSplitter | 保留当前默认 |
| Markdown | MarkdownHeaderTextSplitter | 按标题层级分块，保留标题上下文 |
| HTML | HTMLHeaderTextSplitter | 按 h1/h2/h3 分块 |
| 代码 | RecursiveCharacterTextSplitter（语言特定分隔符） | Python/JS 等保留函数/类完整 |
| PDF/Word | 结构感知：先按页/节，再按语义 | 页码、章节入 metadata |
| CSV/Excel | 按行/按记录，表头重复 | 每块包含表头 |

**语义分块补充**：

- 对长段落使用 **语义切分**（基于 embedding 相似度变化点）作为可选策略。
- 配置 `CHUNK_STRATEGY=semantic|recursive|markdown|auto`。
- `auto` 为默认，根据扩展名自动选择。

**元数据增强**：

- 每个 chunk 增加：
  - `heading`: 所属标题。
  - `page`: 页码（PDF/Word）。
  - `doc_type`: 文件类型。
  - `prev_chunk_id` / `next_chunk_id`: 相邻块，用于上下文扩展。
  - `token_count`: 用于动态上下文预算。

#### 3.2.3 接口变更

- `document_processor.py` 新增 `ChunkingStrategy` 枚举与 `ChunkingFactory`。
- `process_document(file_path, chunk_strategy="auto", chunk_size=None, chunk_overlap=None)`。
- 上传 API 支持前端传入 `chunk_strategy`。

---

### 3.3 上下文层：压缩、重排序与溯源

#### 3.3.1 问题

- `rag_chain.py` 中 `final_context` 直接拼接 `search_context` 和 `formatted_docs`。
- 无上下文长度控制，Top-K 结果多时会超出模型上下文窗口。
- 未处理 “Lost in the Middle” 问题（模型对长上下文中间信息记忆弱）。

#### 3.3.2 方案

**A. 上下文预算管理**

- 根据目标模型上下文窗口（如 8K/32K）设定 `CONTEXT_TOKEN_BUDGET`。
- 预留系统 prompt、历史对话、问题空间后，计算可用于 reference 的 token 数。
- 按 chunk 的 `token_count` 做贪心选择，尽量多放入高相关性 chunk。

**B. 上下文重排序**

- 实现 **Lost in the Middle 重排序**：将高相关 chunk 放在上下文的开头和结尾，低相关放中间。
- 对混合来源（KB + Web）按相关性和来源可信度综合排序。

**C. 上下文压缩（可选）**

- 引入轻量压缩策略：
  - **基于 LLM 的摘要压缩**：对长 chunk 先抽取关键句（可用小模型或同一模型 but 限制输出）。
  - **基于嵌入的语义压缩**：Map-Reduce 式提取与问题相关的子句。
- 配置 `CONTEXT_COMPRESSION_ENABLED` 控制开关，默认 False（先保证基础能力）。

**D. 溯源编号统一**

- KB chunk 与 Web source 统一编号 [1]、[2]…
- 每个编号映射到 `source_metadata`，前端展示来源时统一处理。

#### 3.3.3 实现位置

- 新增 `src/services/context_builder.py`，负责：
  - 融合结果输入。
  - 重排序。
  - 预算截断。
  - 输出 `(context_text, numbered_sources)`。
- `rag_chain.py` 中替换当前 `all_context_parts` 拼接逻辑。

---

### 3.4 路由层：从规则到学习

#### 3.4.1 问题

- `intent_router.py` 完全依赖关键词和正则，新增意图需要修改代码。
- 对“半文档半实时”的混合问题判断不稳定。
- 没有利用历史反馈进行在线学习。

#### 3.4.2 方案

**A. Embedding-based 意图分类**

- 预定义意图类别：
  - `greeting`, `datetime`, `weather`, `price`, `exchange_rate`, `calculation`, `kb_only`, `web_search`, `hybrid`, `agent_research`, `direct_llm`。
- 为每个类别准备 20~50 条示例 query。
- 使用 `bge-m3:latest` 计算 query 与各类别示例的平均相似度，作为一维特征。
- 与规则输出做加权投票：
  - 规则强命中（如 `is_greeting`、`is_datetime_question`）优先规则。
  - 规则未命中时，使用 embedding 分类结果。

**B. 在线学习增强**

- 扩展已有的 `rule_learner.py` / `sample_store.py`：
  - 收集用户反馈（点赞/点踩/来源纠错）。
  - 对“原本路由错误、经人工纠正”的 query 样本定期微调轻量分类器（如 scikit-learn SVM / 少量全连接层）。
  - 由于必须离线，使用本地 embedding + 轻量分类器，不调用 API。

**C. 路由决策可解释性**

- `IntentDecision` 增加 `features` 字段，记录：
  - 命中规则列表。
  - embedding 相似度分数。
  - 历史学习模型分数。
- TraceCollector 持久化这些特征，便于 badcase 分析。

---

### 3.5 生成层：模型分工

#### 3.5.1 问题

- 此前所有任务（改写、分类、摘要、生成、校验）都使用同一个 `deepseek-r1:7b-qwen-distill-q4_K_M`。
- 7B 主模型承担简单结构化任务时延迟高、输出污染风险大。

#### 3.5.2 方案

在 Ollama 本地部署多个模型，按任务分配：

| 任务 | 默认模型 | 说明 |
|------|---------|------|
| 主生成模型 | `deepseek-r1:7b-qwen-distill-q4_K_M` | 复杂推理与最终回答 |
| 轻量任务模型 | `qwen2.5:7b` | 意图路由 LLM 层、Query 改写、会话标题生成 |
| Embedding | `bge-m3:latest` | 多语言稠密向量，1024 维 |
| 重排序 | `qllama/bge-reranker-v2-m3:latest` | KB/Web 检索结果精排，Ollama 本地调用 |
| 事实校验/判断 | 复用 `qwen2.5:7b` | 二分类/抽取任务 |

**实现要点**：

- 在 `config.py` 中通过 `OLLAMA_MODEL_NAME`、`FAST_LLM_MODEL_NAME`、`EMBEDDING_MODEL_NAME`、`EMBEDDING_DIMENSION` 等统一配置。
- 专项任务可通过覆盖配置单独指定模型：
  - `INTENT_ROUTER_LLM_MODEL`
  - `QUERY_REWRITE_MODEL`
  - `TITLE_GENERATION_MODEL`
- 在 `RAGChain` 与相关服务中按需初始化这些模型实例（延迟加载）。
- 对不支持多模型的环境，所有任务回退到主模型，保证兼容性。

---

### 3.6 评估层：指标与评测集

#### 3.6.1 问题

- 测试以 API 单元测试和少量端到端场景为主。
- 没有系统化的检索质量指标和生成质量指标。

#### 3.6.2 方案

**A. 检索指标**

- 构建标准评测集 `tests/evaluation/kb_eval_dataset.jsonl`，每条包含：
  - `question`, `golden_chunk_ids`, `golden_documents`。
- 使用指标：
  - **Hit Rate@K**：golden chunk 是否出现在 Top-K。
  - **MRR@K**：第一个命中结果的倒数排名。
  - **NDCG@K**：考虑相关性排序质量。
  - **Recall@K**：命中 golden 的比例。

**B. 生成指标**

- 对每条评测样本运行 RAGChain，收集：
  - 答案文本。
  - 使用的 sources。
- 使用 **LLM-as-Judge**（本地模型）评估：
  - **Faithfulness**：答案是否忠于检索内容。
  - **Answer Relevance**：答案是否针对问题。
  - **Context Recall**：检索内容是否包含回答问题所需信息。
  - **Citation Precision/Recall**：引用是否准确。

**C. 运行方式**

- `uv run python -m pytest tests/evaluation/test_kb_retrieval.py`：纯检索指标。
- `uv run python tests/evaluation/verify_e2e.py`：端到端验证（天气/时间/搜索场景）。
- 默认 e2e 跳过，CI 可选执行。

**D. 结果看板**

- 将指标写入 Prometheus Gauge：
  - `rag_retrieval_hit_rate`
  - `rag_retrieval_mrr`
  - `rag_answer_faithfulness`
- 在 Grafana 增加面板展示。

---

### 3.7 可观测层：Trace 与 Badcase

#### 3.7.1 当前已有

- `TraceCollector` 已记录：问题、改写后问题、意图、工具调用、最终答案、污染检测、校验结果。
- Prometheus 指标覆盖：向量检索、LLM 调用、知识库查询类型。

#### 3.7.2 增强方案

**A. 检索全链路 Trace**

- 在 `TraceCollector` 中记录：
  - dense search 结果列表（含 score）。
  - sparse search 结果列表（含 score）。
  - RRF 融合后结果。
  - rerank 后结果（含 rerank score）。
  - 最终选入上下文的 chunk IDs。

**B. Badcase 标记 API**

- 新增 `POST /api/feedback/badcase`：
  - 参数：session_id / message_id、badcase_type（`retrieval`, `generation`, `citation`, `routing`）、用户备注。
  - 将对应 trace 标记为 badcase，便于后续分析。

**C. 检索决策日志**

- 每次检索记录日志：
  - `query`, `kb_ids`, `filters`, `dense_top1_score`, `sparse_top1_score`, `rerank_top1_score`, `final_context_token_count`。
- 用于离线分析检索失败模式。

---

## 4. 数据模型与接口变更

### 4.1 数据库/向量库 Schema 变更

**Milvus Collection 新增字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `sparse_embedding` | SPARSE_FLOAT_VECTOR | BM25 稀疏向量 |
| `heading` | VARCHAR(512) | 所属标题 |
| `page` | INT64 | 页码 |
| `doc_type` | VARCHAR(32) | 文档类型 |
| `prev_chunk_id` | VARCHAR(64) | 前一块 ID |
| `next_chunk_id` | VARCHAR(64) | 后一块 ID |
| `token_count` | INT64 | token 数 |

**PostgreSQL 新增表**：

- `retrieval_badcases`：badcase 记录。
- `kb_eval_results`：评测结果持久化。

### 4.2 API 变更

**文档上传**：

```http
POST /api/documents
```

新增可选字段：

```json
{
  "chunk_strategy": "auto",
  "chunk_size": 500,
  "chunk_overlap": 50
}
```

**配置读取/更新**：

```http
GET /api/config/processing
PUT /api/config/processing
```

返回/接收新增字段：

```json
{
  "chunk_strategy": "auto",
  "enable_hybrid_search": true,
  "hybrid_search_top_k": 20,
  "rerank_top_k": 5,
  "context_token_budget": 4000,
  "context_compression_enabled": false
}
```

**Badcase 反馈**：

```http
POST /api/feedback/badcase
```

请求体：

```json
{
  "message_id": "uuid",
  "badcase_type": "retrieval",
  "comment": "未找到相关制度条款"
}
```

---

## 5. 实施路线图

建议分三个阶段实施，每个阶段都可独立测试、独立回退。

### 阶段一：检索增强（高优先级）

- [ ] 在 `milvus_service.py` 中实现 sparse embedding + BM25 检索。
- [ ] 实现 RRF Fusion 与 Cross-Encoder 重排序。
- [ ] 修改 `vector_store.py` 暴露 `search_hybrid` 接口。
- [ ] 在 `rag_chain.py` 中替换 `_retrieve_documents` 为混合检索。
- [ ] 增加配置项与迁移脚本。
- [ ] 新增检索评测集与 `tests/evaluation/test_kb_retrieval.py`。

**预期收益**：KB 检索召回率和 Top-K 精度显著提升。

### 阶段二：上下文与分块优化（中优先级）

- [ ] 实现 `ChunkingStrategy` 工厂，支持 Markdown/代码/表格策略。
- [ ] 增强 chunk metadata（heading/page/doc_type 等）。
- [ ] 实现 `ContextBuilder`：预算管理 + Lost in the Middle 重排序 + 统一溯源编号。
- [ ] 在 `rag_chain.py` 中接入 `ContextBuilder`。
- [ ] 前端上传界面增加分块策略选择。

**预期收益**：长文档回答质量提升，上下文利用率提高。

### 阶段三：路由学习与模型分工（中优先级）

- [ ] 扩展 `intent_router.py`：Embedding-based 分类 + 规则投票。
- [ ] 扩展 `rule_learner.py` 在线学习反馈闭环。
- [ ] 引入轻量子模型（改写/校验/标题），统一模型管理。
- [ ] 完善评估体系：LLM-as-Judge + Grafana 面板。
- [ ] 新增 badcase 反馈 API 与 trace 增强。

**预期收益**：复杂问题路由更准确，整体延迟降低，可解释性增强。

---

## 6. 配置项汇总

新增到 `.env` 与 `config.py` 的配置：

```ini
# 混合检索
KB_ENABLE_HYBRID_SEARCH=true
KB_HYBRID_SEARCH_TOP_K=20
KB_HYBRID_RERANK_TOP_K=5
KB_RRF_K=60

# Milvus HNSW 参数优化
MILVUS_HNSW_M=16
MILVUS_EF_CONSTRUCTION=128
MILVUS_EF=128

# 分块策略
CHUNK_STRATEGY=auto

# 上下文构建
CONTEXT_TOKEN_BUDGET=4000
CONTEXT_COMPRESSION_ENABLED=false

# 模型配置
OLLAMA_MODEL_NAME=deepseek-r1:7b-qwen-distill-q4_K_M
FAST_LLM_MODEL_NAME=qwen2.5:7b
EMBEDDING_MODEL_NAME=bge-m3:latest
EMBEDDING_DIMENSION=1024

# 模型分工（可选，留空则使用 FAST_LLM_MODEL_NAME）
INTENT_ROUTER_LLM_MODEL=
QUERY_REWRITE_MODEL=
TITLE_GENERATION_MODEL=
```

---

## 7. 测试与验收

### 7.1 单元测试

- `tests/test_milvus_service.py`：补充 hybrid search、RRF、rerank 测试。
- `tests/test_document_processor.py`：补充分块策略测试。
- `tests/test_context_builder.py`：新增上下文构建测试。
- `tests/test_intent_router.py`：补充 embedding-based 路由测试。

### 7.2 集成测试

- `tests/evaluation/test_kb_retrieval.py`：检索指标。
- `tests/evaluation/test_rag_quality.py`：生成质量（LLM-as-Judge）。

### 7.3 验收标准

| 指标 | 当前基线 | 阶段一目标 | 阶段二目标 |
|------|---------|-----------|-----------|
| Hit Rate@5 | 未统计 | ≥ 0.75 | ≥ 0.80 |
| MRR@5 | 未统计 | ≥ 0.60 | ≥ 0.65 |
| 平均上下文 token 数 | 未控制 | 可控 | ≤ budget |
| 路由准确率（评测集） | 未统计 | 规则兜底 | ≥ 0.85 |

---

## 8. 风险与回退方案

| 风险 | 应对措施 |
|------|---------|
| Milvus 版本不支持 sparse vector | 检测版本，不支持时自动降级为纯向量检索。 |
| BM25 索引构建耗时长 | 仅在新建集合/迁移脚本时构建；提供 `KB_ENABLE_HYBRID_SEARCH=false` 开关。 |
| Cross-Encoder 增加延迟 | 仅在 KB 模式下对 Top-20 重排；模型可配置为更小的本地模型。 |
| 子模型加载增加内存 | 延迟加载 + 配置可关闭；默认所有任务回退主模型。 |
| 在线学习样本不足 | 先以规则 + embedding 为主，学习模块可独立关闭。 |
| Schema 迁移失败 | 保留旧字段，新增字段默认空；迁移脚本独立运行，失败不影响现有服务。 |

---

## 9. 下一步

本方案为设计稿，待确认后按“阶段一 → 阶段二 → 阶段三”顺序实施。建议优先投入**阶段一（混合检索 + 重排序）**，因为这是当前知识库召回率和精度最明显的短板，且改动相对集中、收益可量化。
