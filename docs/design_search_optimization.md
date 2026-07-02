# 通用联网搜索链路优化方案

> 版本：v1.0
> 日期：2026-06-27
> 状态：待确认

## 一、背景与目标

### 1.1 问题背景

当前项目已对"金价、汇率、天气、时间"等垂类实时查询做了结构化 API 工具化（见 `docs/design_price_trustworthiness.md`），但**通用网页搜索链路**仍存在以下短板：

1. **Query 改写能力弱**：`web_search_service.py` 中的 `SearchQueryRewriter` 虽有基础改写和 LLM 多角度改写，但缺少上下文补全（多轮对话中的代词/省略实体），且改写后 query 不保证保留原始问题，存在召回丢失风险。
2. **搜索结果后处理不够**：`search_postprocessor.py` 已有去重、评分、重排，但缺少"数值提取与交叉验证"的通用化（仅价格场景有 `cross_validate_numeric`），且权威度评分未与最终置信度模型联动。
3. **生成答案无引用保障**：`answer_generator.py` 的 Prompt 已要求"标注来源编号 [n]"，但 7B 模型指令遵循能力有限（实测约 60~70% 遵守率），且无后处理补救机制。
4. **答案校验仅覆盖价格**：`output_sanitizer.py` 的 `NumericHallucinationDetector` 只在有 `reference_numbers` 时生效，通用搜索场景缺少"答案 vs 检索结果"的事实一致性校验。
5. **无多源交叉验证**：金价 4067.98 元/克 问题的根因是搜索结果本身错误，现有方案无法发现"搜索结果自身不可信"的情况。

### 1.2 目标

在不引入付费 API、不破坏现有 SSE/WS 协议的前提下，把通用网页搜索链路升级为：

```
用户问题 + 对话历史
  → 意图路由（垂类工具优先 / 通用搜索）
  → Query Rewriter（规则优先，LLM fallback，含上下文补全）
  → SearXNG 并行搜索（保底包含原始问题）
  → SearchPostprocessor（去重 + 权威度 + freshness + 数值提取）
  → AnswerGenerator（鼓励引用 Prompt）
  → CitationBackfiller（embedding 匹配，自动补引用 [n] / 标记 [?]）
  → AnswerVerifier（分级校验：短答案纯规则，长答案 + LLM 复核）
  → 置信度评分 + 冲突标注
  → SSE 返回
```

### 1.3 设计原则

- **垂类工具优先**：已工具化的场景（价格/天气/时间/汇率）不走通用搜索，避免"搜索抢答"。
- **分级流水线**：80% 简单问题走快速路径（1 次 LLM），20% 复杂问题走完整路径（2~3 次 LLM）。
- **保底策略**：每个模块都有 fallback，单点失败不阻塞整体。
- **不依赖模型自觉**：引用、校验通过后处理强制保障，而非仅靠 Prompt 约束。

---

## 二、现状分析

### 2.1 现有模块盘点

| 模块 | 文件 | 现有能力 | 差距 |
|---|---|---|---|
| Query 改写 | `web_search_service.py` `SearchQueryRewriter` | 基础去停用词 + LLM 多角度改写 + 领域适配 | 缺上下文补全；不保证保留原始问题 |
| 搜索后处理 | `search_postprocessor.py` `SearchPostprocessor` | URL 去重、内容哈希去重、权威度/freshness/relevance 评分、重排 | 数值提取仅用于价格交叉验证；缺通用数值提取 |
| 答案生成 | `answer_generator.py` `AnswerGenerator` | Prompt 模板化，要求标注 [n] | 无后处理引用补救；7B 模型遵守率不稳定 |
| 输出清洗 | `output_sanitizer.py` `OutputSanitizer` | 工具调用 JSON 污染清洗、思考标签移除、数字幻觉检测 | 幻觉检测仅在有 reference_numbers 时生效 |
| 意图路由 | `intent_router.py` `IntentRouter` | 规则识别问候/时间/天气/汇率/价格/计算/研究/KB | 已较完善，需补充快速/完整路径分流 |
| 数据类型 | `search_types.py` `SearchResult` | title/url/content/source/engine | 需扩展 authority_score/freshness_score/source_id 等 |

### 2.2 关键约束

- 本地主模型：`deepseek-r1:7b-qwen-distill-q4_K_M`，单次推理 2~5 秒，指令遵循能力有限。
- 轻量任务模型：`qwen2.5:7b`，用于 Query 改写、意图路由 LLM 层等结构化任务。
- Embedding 模型：`bge-m3:latest`（已通过 OllamaEmbeddings 使用），1024 维稠密向量。
- 搜索引擎：SearXNG（私有化聚合搜索）。
- 延迟容忍：快速路径 ~6 秒，完整路径 ~12 秒（用户已确认可接受）。
- 置信度阈值：0.7（正常）/ 0.4（低置信度警告）（用户已确认先用此值）。
- CitationBackfiller 使用 embedding 匹配（用户已确认）。

---

## 三、详细设计

### 3.1 模块 1：Query Rewriter 增强

**改动文件**：`backend/src/services/query_rewriter.py`（新增独立模块，从 `web_search_service.py` 中抽取并增强）

**当前问题**：
- `SearchQueryRewriter` 内嵌在 `web_search_service.py` 中，职责耦合。
- `rewrite()` 方法不接收对话历史，无法处理多轮对话中的代词/省略实体。
- LLM 改写结果不保证包含原始问题，存在召回丢失风险。

**改进设计**：

```python
class QueryRewriter:
    """查询改写器：规则优先，LLM fallback，支持多轮上下文补全。"""

    async def rewrite(
        self,
        question: str,
        conversation_context: list[dict] | None = None,  # 最近 3 轮 [{role, content}]
    ) -> list[str]:
        """返回改写后的 query 列表，始终包含原始问题作为第一个元素。

        流程：
        1. 上下文补全：检测代词/省略实体，从历史提取实体补全。
        2. 规则快速路径：命中模板（时间/地点/价格/对比）直接生成。
        3. LLM fallback：规则未命中时用 LLM 生成 1~3 个 query。
        4. 保底：queries = [original_question, *rewritten]。
        5. 去重 + 限制最多 4 个。
        """
```

**上下文补全规则**：
- 检测代词：`它|这个|那个|这|那|其|此` → 从上一轮提取核心实体替换。
- 检测省略实体：问题 < 8 字且不含动词 → 从上一轮继承实体。
- 例：上一轮"金价多少" → 追问"那汇率呢" → 补全为"汇率多少"。

**规则快速路径模板**：

| 问题特征 | 生成 query |
|---|---|
| 含"今天/今日 + 价格/金价" | `["{today} {entity} 价格 人民币", "今日{entity} 实时价格"]` |
| 含"天气 + 城市" | `["{city} 今天 天气预报 实时温度", "{city} weather today"]` |
| 含"A 和/与/vs B" | `["{A} 评测", "{B} 评测", "{A} vs {B}"]` |
| 含"最新/最近 + 事件" | `["{event} 最新进展 {year}", "{event} 新闻 {year}"]` |

**LLM Fallback Prompt**：

```
请将用户问题改写为 1-3 个适合搜索引擎的查询短语。
要求：
1. 每个短语聚焦不同角度，覆盖问题的不同方面
2. 必须保留原问题中的核心实体和时间限定词
3. 直接返回 JSON 数组，不要任何解释

对话历史（最近3轮）：
{conversation_context}

用户问题：{question}

输出格式：["查询1", "查询2"]
```

**与现有代码集成**：
- `web_search_service.py` 的 `search_multi()` 改为调用新的 `QueryRewriter.rewrite(question, conversation_context)`。
- `SearchQueryRewriter` 保留为兼容包装，内部委托给新模块。

### 3.2 模块 2：SearchPostprocessor 扩展

**改动文件**：`backend/src/services/search_postprocessor.py`

**新增能力**：

#### 3.2.1 通用数值提取

将 `_extract_numeric_values()` 从私有方法提升为公开方法，供 AnswerVerifier 调用：

```python
def extract_numeric_values(self, text: str) -> list[dict]:
    """从文本中提取数值及其上下文，供交叉验证使用。

    Returns:
        [{"value": Decimal, "raw": "4,067.98", "context": "...金价4067.98元...", "position": int}]
    """
```

#### 3.2.2 多源数值交叉验证

新增方法，检测同一数值是否在多个独立域名中出现：

```python
def cross_source_validate(
    self,
    results: list[SearchResult],
) -> dict[str, list]:
    """对搜索结果中的数值做多源交叉验证。

    Returns:
        {
            "validated_values": [Decimal, ...],   # ≥2 个独立域名一致的数值
            "single_source_values": [...],         # 仅 1 个来源的数值
            "conflicting_values": [...],           # 多源但偏差大的数值
        }
    """
```

**算法**：
1. 对每个 result 提取数值及其上下文。
2. 按"数值相近（±5%）"聚类。
3. 统计每个聚类涉及的独立域名数。
4. 域名数 ≥ 2 → `validated`；= 1 → `single_source`；≥ 2 但偏差 > 5% → `conflicting`。

#### 3.2.3 扩展数据结构

`ScoredResult` 增加字段：

```python
@dataclass
class ScoredResult:
    result: SearchResult
    relevance_score: float = 0.0
    freshness_score: float = 0.0
    authority_score: float = 0.0
    final_score: float = 0.0
    numeric_values: list[Decimal] = field(default_factory=list)
    source_id: int = 0            # 新增：供 CitationBackfiller 引用使用
    domain: str = ""              # 新增：独立域名，供交叉验证
```

### 3.3 模块 3：CitationBackfiller（新增）

**新增文件**：`backend/src/services/citation_backfiller.py`

**职责**：生成答案后，自动补全/校验内联引用，不依赖 LLM。

**设计**：

```python
class CitationBackfiller:
    """引用补全器：用 embedding 匹配为答案中每个事实声明句补上来源编号。"""

    def __init__(self, embeddings):
        """注入 OllamaEmbeddings（复用项目已有的 bge-m3）。"""
        self.embeddings = embeddings

    async def backfill(
        self,
        answer: str,
        sources: list[dict],   # [{"source_index": 1, "title": ..., "content": ...}]
    ) -> str:
        """补全引用并返回处理后的答案。

        流程：
        1. 按句号/问号/感叹号切句。
        2. 逐句判断：
           a. 已有 [n] → 校验 n 是否在有效 source_index 范围内，无效则改为 [?]。
           b. 无引用 → 用 embedding 计算与各 source 的相似度，取最高分。
              - score > 0.65 → 补 [n]
              - score ≤ 0.65 → 补 [?]（未验证标记）
        3. 重组文本返回。
        """
```

**切句规则**：
- 按 `。！？!?` 分隔，保留标点。
- 跳过纯格式行（如 `---`、`###`、列表符号）。
- 跳过 < 5 字的短句（非事实声明）。

**Embedding 匹配优化**：
- 预计算所有 source 的 embedding（一次性批量），缓存复用。
- 逐句计算 query embedding，与 source embedding 做余弦相似度。
- 阈值 0.65 可配置（`settings.search.CITATION_MATCH_THRESHOLD`）。

**输出示例**：

```
今日黄金价格约为 780 元/克[1]。较昨日上涨 0.3%[1]。
国际金价受美元指数影响较大[?]。
建议关注上海黄金交易所实时报价[2]。
```

`[?]` 表示该句未找到对应来源，前端渲染为灰色"未验证"角标。

### 3.4 模块 4：AnswerVerifier（扩展 output_sanitizer.py）

**改动文件**：`backend/src/services/output_sanitizer.py`

**新增类 `AnswerVerifier`**：

```python
@dataclass
class VerificationResult:
    """答案校验结果。"""
    is_consistent: bool
    confidence: float                    # 0~1
    warnings: list[str]
    unsupported_claims: list[str]        # 未在 sources 中找到支撑的声明
    conflicting_claims: list[str]        # 与 sources 冲突的声明
    citation_coverage: float             # 有引用句数 / 总事实句数
    cross_source_consistency: float      # 多源交叉一致的比例


class AnswerVerifier:
    """答案事实一致性校验器。"""

    def __init__(self, embeddings=None):
        self.embeddings = embeddings

    async def verify(
        self,
        answer: str,
        sources: list[dict],
        cross_source_data: dict | None = None,  # 来自 SearchPostprocessor.cross_source_validate
    ) -> VerificationResult:
        """校验答案与检索结果的一致性。

        分级策略：
        - 答案 < 200 字 → 纯规则校验（0 次 LLM）。
        - 答案 ≥ 200 字 → 规则初筛 + LLM 复核（1 次 LLM）。
        """
```

**校验内容**：

1. **数字一致性**：
   - 提取答案中的所有数字（复用 `NumericHallucinationDetector.extract_numbers`）。
   - 与 sources 中的数字集合对比。
   - 偏差 > 5% 且 sources 中无相近值 → `unsupported_claim`。

2. **日期一致性**：
   - 提取答案中的日期（正则匹配 `\d{4}年|\d{4}-\d{2}-\d{2}|今天|昨日|本周`）。
   - 与 sources 中的日期对比。
   - 答案中出现的日期在 sources 中完全不存在 → `warning`。

3. **引用真实性**：
   - 提取答案中的 `[n]`。
   - 校验 n 是否在有效 source_index 范围内。
   - 无效引用 → `warning`。

4. **多源交叉一致性**（使用 `cross_source_data`）：
   - 答案中的关键数值是否在 `validated_values` 中 → 高置信。
   - 仅在 `single_source_values` 中 → 中置信。
   - 在 `conflicting_values` 中 → 低置信 + warning。

**置信度计算**：

```python
confidence = (
    0.4 * cross_source_consistency   # 多源交叉一致性
    + 0.3 * citation_coverage         # 引用覆盖率（有 [n] 的句子占比，[?] 不计入）
    + 0.2 * source_authority          # 来源平均权威度
    + 0.1 * freshness_score           # 时效性
)
```

| 因子 | 计算方式 |
|---|---|
| `cross_source_consistency` | 答案关键数值在 ≥2 个独立域名出现的比例 |
| `citation_coverage` | `有 [n] 引用句数 / 总事实句数`（`[?]` 不计入分子） |
| `source_authority` | 引用来源的权威度均值（复用 `_DOMAIN_AUTHORITY`） |
| `freshness_score` | 来源平均新鲜度（今天=1.0，一周内=0.7，一月内=0.4，更早=0.1） |

**阈值与输出**：
- `confidence >= 0.7` → 正常返回。
- `0.4 <= confidence < 0.7` → 答案末尾追加：`⚠️ 部分信息来源单一或时效性存疑，建议核实。`
- `confidence < 0.4` → 答案末尾追加：`⚠️ 当前回答可信度较低，未在多个独立来源中得到验证，请谨慎参考。`

### 3.5 模块 5：AnswerGenerator Prompt 改造

**改动文件**：`backend/src/services/answer_generator.py`

**改动点**：将 Prompt 中的"必须标注来源编号"改为"鼓励标注"。

**原 Prompt 片段**：
```
3. 关键事实必须标注来源编号，如[1]、[2]，对应参考信息中的来源编号。
```

**改为**：
```
3. 尽量在事实性陈述后标注来源编号，如[1]、[2]，对应参考信息中的来源编号。
   如果不确定来源编号，可以不标注，系统会自动补全。
```

**原因**：7B 模型对"必须"的遵守率不稳定，改为"鼓励"+ 后处理 `CitationBackfiller` 补全，整体引用覆盖率更稳定。

**新增 `generate_with_citation` 方法**：

```python
async def generate_with_citation(
    self,
    question: str,
    history_context: str = "",
    tool_results=None,
    kb_docs=None,
    kb_source_metadata=None,
    search_sources: list[dict] | None = None,  # 新增：搜索来源，供 CitationBackfiller 使用
    is_realtime: bool = False,
) -> tuple[str, list[dict]]:
    """生成答案并自动补全引用。

    流程：
    1. 调用 generate() 生成原始答案。
    2. 调用 CitationBackfiller.backfill() 补全引用。
    3. 返回 (answer_with_citations, sources)。
    """
```

### 3.6 模块 6：链路整合

**改动文件**：`backend/src/services/rag_chain.py`（主链路）

**整合流程**：

```python
async def handle_web_search_question(
    self,
    question: str,
    history: list[dict],
    ...
):
    # 1. 意图路由（垂类工具优先，已由 IntentRouter 实现）
    decision = self.intent_router.route(question, ...)
    if decision.primary_mode == PrimaryMode.TOOL_FIRST:
        return await self.handle_tool_first(decision, question, history)

    # 2. 通用搜索
    # 2a. Query 改写
    queries = await self.query_rewriter.rewrite(question, conversation_context=history[-3:])

    # 2b. 并行搜索（已由 web_search_service.search_multi 实现）
    results = await self.web_search_service.search_multi_with_queries(queries)

    # 2c. 后处理（含数值提取、交叉验证）
    processed = self.postprocessor.process(results, top_k=self.context_results)
    cross_source_data = self.postprocessor.cross_source_validate(processed)

    # 2d. 构建上下文 + 来源
    context, sources = self._build_context_with_ids(processed)

    # 2e. 生成（鼓励引用 Prompt）
    answer, sources = await self.answer_generator.generate_with_citation(
        question=question,
        history_context=history_text,
        search_sources=sources,
    )

    # 2f. 校验
    verification = await self.answer_verifier.verify(
        answer=answer,
        sources=sources,
        cross_source_data=cross_source_data,
    )

    # 2g. 追加置信度警告
    if verification.confidence < 0.4:
        answer += "\n\n⚠️ 当前回答可信度较低，未在多个独立来源中得到验证，请谨慎参考。"
    elif verification.confidence < 0.7:
        answer += "\n\n⚠️ 部分信息来源单一或时效性存疑，建议核实。"

    # 2h. SSE 返回
    return answer, sources, verification
```

### 3.7 SSE 协议扩展

不破坏现有协议，仅新增可选字段：

```json
{
  "event": "answer_end",
  "data": {
    "answer": "...",
    "sources": [...],
    "search_status": "done",
    "confidence": 0.72,
    "verification_warnings": ["答案中数值 4067.98 未在搜索结果中找到"],
    "query_rewrite_info": {
      "original": "今天金价",
      "rewritten": ["2026-06-27 黄金价格 人民币/克", "今日金价 上海黄金交易所"]
    }
  }
}
```

前端行为：
- `confidence < 0.7` → 显示警告条。
- `query_rewrite_info` → 可折叠展示"搜索使用了以下关键词"。
- `[?]` 标记 → 渲染为灰色"未验证"角标。
- 现有不使用这些字段的客户端不受影响。

---

## 四、分级流水线设计

### 4.1 快速路径（80% 流量）

**触发条件**（命中任一）：
- 问题 < 15 字。
- 命中规则模板（时间/地点/价格/天气/对比关键词）。
- 非多实体问题（不含"和/与/vs/哪个"）。
- 非多轮对话追问（无代词/省略实体）。

**流程**：
```
规则 Rewriter（0 LLM）→ 单次 SearXNG → Postprocessor → Generator（1 LLM）→ 规则 CitationBackfiller + Verifier（0 LLM）
```

**延迟**：~6 秒（1 次 LLM 调用）。

### 4.2 完整路径（20% 复杂问题）

**触发条件**：
- 规则未命中。
- 含"对比、分析、调研、最新进展"等复杂意图。
- 多轮对话追问（需上下文补全）。

**流程**：
```
LLM Rewriter（1 LLM）→ 多 query 并行 SearXNG → Postprocessor + 交叉验证 → Generator（1 LLM）→ embedding CitationBackfiller → LLM Verifier（1 LLM）
```

**延迟**：~12 秒（3 次 LLM 调用）。

### 4.3 降级矩阵

| 情况 | 处理 |
|---|---|
| 所有 query 搜索均返回空 | 告知"未找到相关网页"，询问是否用模型知识回答 |
| 部分 query 超时 | 用已返回的结果继续，不阻塞 |
| Rewriter 失败 | 用原始问题搜索 |
| CitationBackfiller 失败 | 跳过补全，答案照常返回 |
| Verifier 失败 | 跳过校验，不追加警告 |
| 正文抽取失败 | 退化为 snippet 作为 source 内容 |
| Embedding 服务不可用 | CitationBackfiller 退化为关键词匹配 |

---

## 五、数据结构变更

### 5.1 新增/修改文件

| 文件 | 动作 | 说明 |
|---|---|---|
| `backend/src/services/query_rewriter.py` | 新增 | 独立 Query 改写器，含上下文补全 |
| `backend/src/services/citation_backfiller.py` | 新增 | 引用补全器，embedding 匹配 |
| `backend/src/services/search_postprocessor.py` | 修改 | 新增 `extract_numeric_values`、`cross_source_validate`、扩展 `ScoredResult` |
| `backend/src/services/output_sanitizer.py` | 修改 | 新增 `AnswerVerifier`、`VerificationResult` |
| `backend/src/services/answer_generator.py` | 修改 | Prompt 改为鼓励引用，新增 `generate_with_citation` |
| `backend/src/services/web_search_service.py` | 修改 | 接入新 QueryRewriter，`search_multi` 传入 conversation_context |
| `backend/src/services/rag_chain.py` | 修改 | 整合完整链路 |
| `backend/src/services/intent_router.py` | 修改 | 新增快速/完整路径分流判断方法 |
| `backend/src/services/search_types.py` | 修改 | `SearchResult` 可选扩展 source_id/domain 字段 |

### 5.2 新增测试文件

| 文件 | 说明 |
|---|---|
| `backend/tests/test_query_rewriter.py` | 规则路径、LLM fallback、上下文补全测试 |
| `backend/tests/test_citation_backfiller.py` | 切句、embedding 匹配、已有引用校验、[?] 标记 |
| `backend/tests/test_answer_verifier.py` | 数字/日期/引用一致性、置信度计算、分级警告 |
| `backend/tests/evaluation/test_search_pipeline.py` | 端到端搜索链路评估（mock SearXNG） |

---

## 六、配置项

在 `settings.search` 中新增（均有默认值，零配置可用）：

```python
# 引用补全
CITATION_MATCH_THRESHOLD: float = 0.65       # embedding 相似度阈值
CITATION_ENABLE_EMBEDDING: bool = True        # 是否用 embedding 匹配（False 退化为关键词）

# 答案校验
ANSWER_VERIFIER_LLM_THRESHOLD: int = 200      # 答案字数超过此值才触发 LLM 复核
ANSWER_CONFIDENCE_WARNING: float = 0.7        # 正常阈值
ANSWER_CONFIDENCE_LOW: float = 0.4            # 低置信度阈值

# Query 改写
QUERY_REWRITE_MAX_QUERIES: int = 4            # 改写后最多保留的 query 数
QUERY_REWRITE_CONTEXT_TURNS: int = 3          # 上下文补全回溯轮数
```

---

## 七、测试方案

### 7.1 单元测试

**test_query_rewriter.py**：
- 规则路径：`"今天金价"` → 生成含日期的 query。
- 规则路径：`"北京和上海天气"` → 拆分为两个城市 query。
- LLM fallback：无规则命中时调用 LLM，mock 返回 JSON 数组。
- 上下文补全：`"那汇率呢"` + 历史 `["金价多少"]` → 补全为 `"汇率多少"`。
- 保底：queries 始终包含原始问题。

**test_citation_backfiller.py**：
- 已有引用校验：`[1]` 有效、`[9]` 无效改为 `[?]`。
- 无引用补全：embedding 相似度 > 0.65 → 补 `[n]`。
- 无引用且无匹配：补 `[?]`。
- 短句跳过：`< 5 字` 不处理。
- 格式行跳过：`### 标题` 不处理。

**test_answer_verifier.py**：
- 数字一致：答案 `780` 在 sources 中 → 高置信。
- 数字不一致：答案 `4067.98` 不在 sources 中 → `unsupported_claim`。
- 日期越界：答案 `2024年` 但 sources 中无 → warning。
- 引用不存在：`[9]` 无效 → warning。
- 置信度分级：`>= 0.7` 无警告；`0.4~0.7` 追加提醒；`< 0.4` 追加严重警告。

### 7.2 集成测试

- mock SearXNG 返回旧数据（2024 年金价），验证答案中不再出现 2024 年的"今日金价"。
- mock 多源冲突（A 说 780，B 说 4067），验证答案末尾出现置信度警告。
- mock 搜索失败，验证降级提示。

### 7.3 回归测试

- 运行现有 `test_price_data.py`、`test_price_scenarios.py`，确保价格链路不受影响。
- 运行现有 datetime/weather 工具测试，确保垂类工具优先级不变。

---

## 八、实现顺序

按以下顺序逐一实现，每个模块独立可测：

1. **P0-a**：`query_rewriter.py` + 单测
2. **P0-b**：`search_postprocessor.py` 扩展 + 单测
3. **P0-c**：`citation_backfiller.py` + 单测
4. **P0-d**：`output_sanitizer.py` 扩展 `AnswerVerifier` + 单测
5. **P0-e**：`answer_generator.py` Prompt 改造 + `generate_with_citation`
6. **P0-f**：`web_search_service.py` / `rag_chain.py` 链路整合
7. **P1**：`intent_router.py` 快速/完整路径分流
8. **P1**：端到端评估测试

---

## 九、风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| Embedding 服务不可用 | 中 | CitationBackfiller 失效 | 退化为关键词匹配 |
| LLM Verifier 增加延迟 | 高 | 完整路径 12 秒 | 仅长答案触发 LLM 复核，短答案纯规则 |
| 7B 模型改写质量差 | 中 | 搜索召回不足 | 保底包含原始问题；规则路径覆盖常见场景 |
| 多源交叉验证误判 | 低 | 置信度偏差 | 阈值可配置，后续 evaluation set 调参 |
| SSE 新字段前端未适配 | 低 | 前端忽略未知字段 | 字段均为可选，向后兼容 |

---

## 十、验收标准

1. 通用搜索场景下，答案中 `[n]` 引用覆盖率 ≥ 90%（含 CitationBackfiller 补全）。
2. 答案中的关键数值（价格/日期/百分比）在 sources 中可追溯的比例 ≥ 85%。
3. 搜索结果为旧数据时，答案不再直接引用过期数据（被 freshness 评分降权或 Verifier 标记）。
4. 置信度 < 0.4 的回答，末尾必须出现警告提示。
5. 快速路径延迟 ≤ 8 秒（含网络波动），完整路径 ≤ 15 秒。
6. 现有价格/天气/时间垂类工具测试全部通过，无回归。
