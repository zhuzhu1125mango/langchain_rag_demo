# 决策模型（Intent Router）升级设计方案

> 状态：已实施（`backend/src/services/intent_router/`：LLMRouter / ConfidenceGate / 历史上下文增强）

## 1. 背景与目标

### 1.1 当前现状
当前 `IntentRouter` 采用纯规则驱动：
- 基于关键词集合判定意图（问候、实时性、计算、研究、知识库优先等）
- 硬编码 `if-else` 优先级分支
- 仅有 `classify_search_pipeline` 使用了对话历史
- 缺乏对决策结果的量化评估与反馈机制

### 1.2 主要问题
1. **泛化能力差**：关键词覆盖不全，容易误匹配/漏匹配（如"帮我查下这个"既可指知识库也可指搜索）
2. **意图互斥假设不合理**：很多问题同时具备 KB + Web + Tool 多维度需求
3. **无置信度**：无法识别低置信度请求并主动澄清
4. **历史上下文利用不足**：主路由未利用多轮信息做指代消解和话题继承
5. **缺乏反馈闭环**：路由决策好坏无从度量，难以持续优化
6. **职责边界模糊**：计算类、汇率类、价格类工具存在重叠匹配风险

### 1.3 设计目标
- 在 **不破坏现有接口** 的前提下，将路由准确率从规则关键词级别提升到语义理解级别
- 引入 **置信度评分 + 多标签分类**，支持混合意图
- 增强 **历史上下文感知** 与 **会话状态管理**
- 建立 **路由效果评估体系** 与 **持续反馈优化机制**
- 保持 **低延迟快速路径** 与 **高质量慢速路径** 的可选降级

## 2. 设计原则

1. **向后兼容**：`IntentRouter.route(...)` 接口不变，新增字段可选
2. **规则兜底**：常见高频场景仍走规则 fast-path，LLM 路由作为增强层
3. **可解释性**：每个决策必须附带 `reasoning` 和置信度分数
4. **可评估**：所有决策落库，支持离线指标计算
5. **低成本优先**：默认使用本地小模型，复杂场景才启用大模型

## 3. 总体架构

```
                    ┌─────────────────────────────────────┐
                    │           IntentRouter              │
                    │  (统一入口，保持现有接口)            │
                    └──────────────┬──────────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          │                        │                        │
          ▼                        ▼                        ▼
┌───────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
│  FastRuleRouter   │  │   LLMRouter         │  │  ConfidenceGate     │
│  规则快速路由      │  │   语义理解路由       │  │  置信度门控/澄清     │
└───────────────────┘  └─────────────────────┘  └─────────────────────┘
          │                        │                        │
          └────────────────────────┼────────────────────────┘
                                   ▼
                    ┌─────────────────────────────────────┐
                    │      IntentDecision (增强版)         │
                    │  - primary_mode                      │
                    │  - confidence_scores                 │
                    │  - suggested_tools                   │
                    │  - fallback_strategy                 │
                    │  - search_pipeline                   │
                    │  - context_rewrite / needs_clarify   │
                    └─────────────────────────────────────┘
```

## 4. 详细模块设计

### 4.1 FastRuleRouter（规则快速层）

保留并优化现有规则：
- 问候语、空输入、明显的时间/天气/汇率/价格/计算等高频确定性意图
- 规则命中时直接返回，不调用 LLM，保证 < 10ms 延迟
- 规则未命中或规则间冲突时，进入 LLMRouter

新增能力：
- **规则冲突检测**：多个规则同时命中时，标记为 ambiguous，转 LLM 裁决
- **规则优先级可配置**：通过配置文件调整规则覆盖顺序

### 4.2 LLMRouter（语义理解层）

新增基于本地 LLM 的语义分类器。

#### 4.2.1 模型选择
- 默认使用轻量模型 `qwen2.5:7b`（通过 `FAST_LLM_MODEL_NAME` 配置，也可通过 `INTENT_ROUTER_LLM_MODEL` 单独覆盖）
- 轻量模型在保证语义理解能力的同时，延迟远低于主推理模型 `deepseek-r1:7b-qwen-distill-q4_K_M`

#### 4.2.2 Prompt 设计
Prompt 要求模型输出结构化 JSON：

```json
{
  "needs_kb": 0.8,
  "needs_web": 0.9,
  "needs_realtime": 0.95,
  "needs_tool": 0.9,
  "primary_mode": "hybrid",
  "suggested_tools": ["gold_price", "exchange_rate"],
  "search_pipeline": "fast_path",
  "needs_clarify": false,
  "clarify_question": "",
  "context_rewrite": "2026年6月28日黄金价格和美元兑人民币汇率",
  "reasoning": "用户询问金价和汇率，属于强实时性+数值型问题，建议使用专用工具。"
}
```

各字段含义：
- `needs_*`：0~1 的置信度分数
- `primary_mode`：最终主模式
- `suggested_tools`：推荐工具列表
- `search_pipeline`：联网搜索分级路径
- `needs_clarify`：是否需要反问澄清
- `context_rewrite`：结合历史补全后的标准问题
- `reasoning`：可解释性说明

#### 4.2.3 Few-Shot 示例库
维护 `backend/prompts/intent_router_examples.jsonl`，包含：
- 问题
- 历史上下文（可选）
- 期望输出

支持热加载，便于运营调优。

### 4.3 ConfidenceGate（置信度门控）

对 LLMRouter 输出做后处理：

| 条件 | 处理动作 |
|------|---------|
| 所有 `needs_*` 分数均 < 0.4 | 标记为 `DIRECT_LLM`， reason="置信度过低，直接由 LLM 回答" |
| 最高分两意图分差 < 0.15 | 触发澄清流程 `needs_clarify=true` |
| `needs_kb` > 0.6 但无可用知识库 | 自动提升 `needs_web` 并降级为 web search |
| `needs_realtime` > 0.7 但未开启联网搜索 | 设置 `fallback_strategy=TELL_FAILURE` |

### 4.4 历史上下文增强

新增 `ConversationContext` 组件：
- 接收最近 N 轮对话
- 提取当前话题领域、已使用工具、已检索文档
- 对含代词/省略的问题做上下文补全（如"它多少钱？" → "黄金价格多少钱？"）

输入到 LLMRouter 的历史格式：
```json
[
  {"role": "user", "content": "今天金价多少？"},
  {"role": "assistant", "content": "今天黄金价格为 780 元/克。"},
  {"role": "user", "content": "那美元汇率呢？"}
]
```

### 4.5 工具选择精细化

为每个工具定义元数据：

```python
@dataclass
class ToolMeta:
    name: str
    description: str
    trigger_patterns: List[str]
    required_entities: List[str]
    examples: List[str]
    conflict_priority: int
```

例如：
- `exchange_rate`：priority=10，required_entities=["货币对"]
- `calculator`：priority=5，用于纯数学表达式

路由时若多个工具命中，按 priority 和实体完整度选择，必要时组合调用。

## 5. 数据流

```
用户提问
   │
   ▼
FastRuleRouter ──命中──▶ IntentDecision
   │ 未命中/冲突
   ▼
ConversationContext ──▶ 历史压缩 + 指代补全
   │
   ▼
LLMRouter ──▶ JSON 决策 + 置信度
   │
   ▼
ConfidenceGate ──▶ 门控/澄清/降级
   │
   ▼
IntentDecision（最终）
   │
   ▼
ToolExecutor / RAGChain / AnswerGenerator
```

## 6. IntentDecision 增强

在现有字段基础上新增：

```python
@dataclass
class IntentDecision:
    needs_kb: bool = False
    needs_web: bool = False
    needs_realtime: bool = False
    primary_mode: PrimaryMode = PrimaryMode.DIRECT_LLM
    suggested_tools: List[str] = field(default_factory=list)
    fallback_strategy: FallbackStrategy = FallbackStrategy.NONE
    reasoning: str = ""
    search_pipeline: SearchPipeline = SearchPipeline.FAST_PATH
    
    # 新增字段
    confidence_scores: Dict[str, float] = field(default_factory=dict)
    needs_clarify: bool = False
    clarify_question: str = ""
    context_rewrite: str = ""
    rule_hit: bool = False
    llm_routed: bool = False
```

## 7. 评估方案

### 7.1 离线评估集
新建 `backend/tests/evaluation/test_intent_router.py`，覆盖：
- 50+ 条典型问题（问候、天气、金价、汇率、计算、KB、Web、Hybrid、Agent、歧义）
- 每条标注期望 primary_mode 和工具
- 评估指标：
  - **Primary Mode Accuracy**：主模式选择正确率
  - **Tool Hit Rate**：推荐工具命中正确率
  - **Confidence Calibration**：置信度与正确率的相关性
  - **Clarify Trigger Rate**：需要澄清时被正确触发的比例

### 7.2 在线指标
每次路由决策写入 trace 表：
- 输入问题、历史、决策结果、置信度
- 实际执行路径
- 是否触发 fallback
- 用户反馈（点赞/点踩）

定期生成报告，用于 prompt 调优和 few-shot 更新。

## 8. 实施计划

### Phase 1：基础增强（1~2 天）
1. 新增 `LLMRouter` 类和 prompt 模板
2. 实现 `ConfidenceGate` 门控逻辑
3. 修改 `IntentRouter.route()`，引入 rule → llm → gate 三层流程
4. 保持向后兼容，所有新增字段可选

### Phase 2：上下文与工具精细化（2~3 天）
1. 新增 `ConversationContext` 历史压缩/指代补全
2. 定义工具元数据，优化工具冲突解决
3. 扩展 `IntentDecision` 字段
4. 更新下游调用方使用 `context_rewrite`

### Phase 3：评估与反馈闭环（2~3 天）
1. 构建 50+ 条评估集
2. 在 trace 中记录路由决策与执行结果
3. 实现离线评估脚本
4. 根据评估结果迭代 prompt 和 few-shot

## 9. 风险与降级

| 风险 | 降级方案 |
|------|---------|
| LLM 调用超时/失败 | 自动回退到 FastRuleRouter |
| LLM 输出 JSON 解析失败 | 使用正则兜底提取关键字段，失败则走 DIRECT_LLM |
| 置信度过低 | 标记为 DIRECT_LLM，避免错误工具调用 |
| 新增字段下游未消费 | 新增字段默认空值，不影响旧逻辑 |
| 延迟增加 | 规则 fast-path 覆盖 70%+ 高频场景，仅复杂问题走 LLM |

## 10. 接口兼容性

`IntentRouter.route(...)` 签名保持向后兼容，方法改为异步：

```python
async def route(
    self,
    question: str,
    kb_ids: Optional[List[str]] = None,
    use_web_search: bool = False,
    search_mode: str = "simple",
    history: Optional[List[Dict]] = None,
) -> IntentDecision:
```

内部新增 `use_llm` 开关（默认 True），支持通过配置关闭 LLM 路由：

```bash
INTENT_ROUTER_USE_LLM=true
INTENT_ROUTER_LLM_TIMEOUT=3.0
INTENT_ROUTER_CONFIDENCE_THRESHOLD=0.4
INTENT_ROUTER_LLM_MODEL=                  # 留空则使用 FAST_LLM_MODEL_NAME
```

---

## 下一步

待确认本方案后，进入 Phase 1 实现：先实现 `LLMRouter` + `ConfidenceGate`，并更新 `IntentRouter.route()` 的三层决策流程。
