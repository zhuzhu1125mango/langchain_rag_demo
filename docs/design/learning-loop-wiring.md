# 学习闭环接入（LEARN）：设计

> 状态：已实施（W-A/W-B/W-C 2026-10-16 落地，单测+dev 端到端验证通过；三项开关灰度默认关）
> 关联：[project-status.md](../project-status.md)（§二 LEARN / §四 红线）、[archive/project-readthrough-2026-09-30.md](../archive/project-readthrough-2026-09-30.md)（P0-2 / P0-4）、[rag-enhancement.md](rag-enhancement.md) §3.4「路由层：从规则到学习」、[agent-ab-evaluation.md](agent-ab-evaluation.md)
> 日期：2026-10-16
> 说明：遵循「设计先行 → 确认后实施」。目标是把已核实「空转」的学习闭环与 A/B 实验框架接进真实问答链路。
>
> **决策点（2026-10-16 已定）**：D1 含 `llm_inference`（fast 模型）；D2 配置指定实验 `AB_CHAT_EXPERIMENT_ID`；D3 白名单含召回参数（启用前须先离线 A/B）；D4 接入 chat 路径。

## 1. 问题定位：学习闭环三重断链

| # | 断链 | 现状（2026-10-16 复核） | 锚点 |
|---|---|---|---|
| ① | **训练输入恒空** | 用户勾选知识库时决策短路，直接返回 `strategy_results={}`；`chat.py` 上报的样本也就恒为空 → `calculate_strategy_accuracy` 无输入 → 权重调整恒空 | [decision_pipeline.py](../../backend/src/services/decision_pipeline.py) L143-150；[config.py](../../backend/src/config.py) L427 `DECISION_VOTE_WHEN_KB_SELECTED=False`；[rule_learner.py](../../backend/src/services/rule_learner.py) L61-91 |
| ② | **权重存不住 + 回载缺失** | 学习时新建**临时** `StrategyManager`（非线上实例），调整落在临时对象；`update_strategy_config` 虽写入 `strategy_configs` 表，但 `create_default_strategy_manager()` 永远返回硬编码默认权重，`get_strategy_config` 无任何调用方 → 学到的权重永不被回载 | [strategy_manager.py](../../backend/src/services/strategy_manager.py) L248-267 / L22-27；[main.py](../../backend/src/main.py) L154；[api/learning.py](../../backend/src/api/learning.py) L86；[sample_store.py](../../backend/src/services/sample_store.py) L312（`get_strategy_config` 无调用方） |
| ③ | **A/B 分流不影响回答** | `experiment_manager.allocate_traffic` 仅被 `api/experiment.py` 调用，`pipeline/*` / `rag_chain.py` / `kb_retrieval_service.py` 无任何分流或变体读取 → 实验是「离线记录系统」 | [experiment_manager.py](../../backend/src/services/experiment_manager.py) L287；[api/experiment.py](../../backend/src/api/experiment.py) L138-152 |

补充事实：写侧**已通**——`chat.py`（流式 + 非流式）都调用 `learning_engine.record_execution` 落 `strategy_executions`；反馈经 `/learning/record-feedback` 落 `strategy_feedbacks`。故问题只在读/学习/生效侧。

**影响面**：`should_use_knowledge_base`（策略投票兜末级）仅在「勾选 KB 且 `DECISION_VOTE_WHEN_KB_SELECTED=True`」时才走到；默认 `False` ⇒ 正常路径不执行投票。因此本方案的实际收益落在「KB 门控决策可被反馈微调」，价值有限但闭环真实。

## 2. 目标与非目标

**目标**
- 学习闭环可真实闭环：权重**可回载**并作用于在线决策；A/B 实验分流**可作用于在线问答**（含指标回收）。
- 全部改动**灰度默认关**，不改动默认行为，可一行配置回退。

**非目标**
- 不引入新模型、不改变「4B 仅 deep_thinking 主回答链」的显存铁律。
- 不重构策略体系本身；不对主检索排序做任何混入（本方案的权重只影响「是否用 KB」的门控投票，不改变检索候选）。

## 3. 方案（三项工作项）

### W-A 单例化 + 权重回载（闭合断链②，核心）

- 新增进程内访问器 `get_strategy_manager()`（复用 [AsyncSingleton](../../backend/src/utils/async_singleton.py) 或模块级缓存），作为**唯一**线上策略管理器实例。
- `StrategyManager` 新增 `load_from_store()`：按策略名经 `sample_store.get_strategy_config` 读取 `weight/enabled`，并回载决策阈值；读不到的键保持默认。
- 生命周期：`main.py` lifespan 启动时，若开关开，则 `get_strategy_manager()` → `load_from_store()`。
- 学习侧统一改用该单例：`_periodic_learning_task`（[main.py](../../backend/src/main.py) L154）与 `api/learning.py` 的 `trigger_learning`（L86）不再 `create_default_strategy_manager()` 新建实例 → 学习调整直接落到线上实例，且同时持久化回 DB。
- `rag_chain._async_init` 的 `strategy_manager` 也改为取该单例（保证与学习侧同一对象）。

**开关**：`LEARNING_WEIGHT_RELOAD_ENABLED`（`DecisionSettings`，默认 `false`）。

### W-B 样本输入修复（闭合断链①）

KB 勾选短路路径不再上报空 `strategy_results`，改为**后台补采**（不改决策、不加首字延迟）：
- 决策完成后派发后台任务，执行全部策略并把结果写入 `strategy_results` 落库。`keyword` / `context` 为纯规则零成本；`llm_inference` 已是 `fast` 模型 + `think=False` + `STRATEGY_LLM_TIMEOUT`（5s）上限，符合红线。
- 后台任务失败不影响回答与消息落库（同现有 `record_execution` 的 best-effort 语义）。

**开关**：`LEARNING_SAMPLE_COLLECT_BG_ENABLED`（默认 `false`）。

> **D1 已定（2026-10-16）**：纳入 `llm_inference`。

### W-C A/B 分流接入 chat 路径（闭合断链③）

- 在 `chat` 回答前解析「本请求适用的运行中实验」→ `allocate_traffic(experiment_id, user_id, session_id)` 取变体 → 按**白名单**把 `variant.config` 映射到问答参数；回答后 `record_metric`（如 `response_time`，可选 `accuracy`）。
- 实验来源：新增配置项 `AB_CHAT_EXPERIMENT_ID` 指定单一实验（部署级）；未配置或实验非 running 时不分流。
- 可实验化参数**白名单**：`search_mode`、`KB_HYBRID_RERANK_TOP_K` 等**召回参数**、策略权重（经 W-A 单例）、`DECISION_VOTE_WHEN_KB_SELECTED` 等**决策参数**；**排除**任何改变答案安全边界的项（模型名、温度、系统提示）。
- 变体 id 落 message 字段便于归因（**不新增 trace.data 键**）。

**开关**：`AB_EXPERIMENT_CHAT_ENABLED`（默认 `false`）。

> **D2 已定（2026-10-16）**：配置指定实验 `AB_CHAT_EXPERIMENT_ID`。
> **D3 已定（2026-10-16）**：白名单含召回参数；因命中「混入主检索必先 A/B」红线，召回类变体在**离线 A/B 通过前不得默认启用**。
> **D4 已定（2026-10-16）**：接入 chat 路径（不摘下）。

## 4. 开关与灰度汇总

| 开关 | 归属 | 默认 | 作用 |
|---|---|---|---|
| `LEARNING_WEIGHT_RELOAD_ENABLED` | DecisionSettings | false | 启动/学习后回载持久化权重 |
| `LEARNING_SAMPLE_COLLECT_BG_ENABLED` | DecisionSettings | false | KB 短路路径后台补采策略结果 |
| `AB_EXPERIMENT_CHAT_ENABLED` | SearchSettings | false | chat 路径接入实验分流 |

三项默认关 ⇒ 合入零行为变更；逐项可独立灰度与回退。

## 5. 红线合规

1. **4B 铁律**：W-B 的 LLM 策略与 W-C 的指标记录均为辅助任务，一律走 `fast` 模型/无模型；不触碰 4B。
2. **混入主检索必先 A/B**：W-A/W-B 影响的是「是否用 KB」的**门控投票**，不改变检索候选与排序，不属主检索混入；W-C 的变体白名单若含召回参数（如 `KB_HYBRID_RERANK_TOP_K`），须先离线 A/B 后启用。
3. **不新增 trace.data 键**：W-C 归因走既有 `trace.stages` 明细或 message 字段，避免重蹈「RequestTrace 固定列」覆辙（见 [e1-navigable-lookup-gap.md](e1-navigable-lookup-gap.md) TRACE 修复）。

## 6. 验证

- 单测：
  - `load_from_store` 回载与缺省保持（含 `get_strategy_config` 首次被调用）。
  - 单例一致性：`get_strategy_manager()` 与 `rag_chain.strategy_manager`、学习侧为同一对象。
  - W-B：短路路径后台补采写入非空 `strategy_results`；开关关时行为不变。
  - W-C：`allocate_traffic` 在 chat 路径被调用、白名单映射生效、变体落库；开关关时不调用。
- 集成：`test_learning_*` / `test_experiment.py` 扩展；后端全量回归（当前基线 1032 passed / 103 skipped）。
- 端到端（W-C 开启时）：最小样本量下的分流命中与指标回收核对。

## 7. 风险与回退

| 风险 | 处置 |
|---|---|
| 回载权重改变线上 KB 门控行为 | 默认关；开启后按 `strategy_executions` 指标对比；回退 = 关开关 + `update_strategy_config` 复位 |
| 后台补采引入额外 fast 模型调用 | LLM 策略已是 `fast` + `think=False` + 5s 超时；开关独立可关 |
| A/B 变体参数越权/误配 | 白名单强校验 + 启动期实验校验 + 变体落库归因 |
| 学习样本仍稀疏（需 ≥50 标注） | 属预期：慢速后台回路，不阻塞；README 保留「写回闭环待打通」表述直至真实验证 |

## 8. 实施顺序

**顺序**：W-A（核心闭环）→ W-B（喂数据）→ W-C（A/B 接入）→ 文档/台账回写。

**决策点（2026-10-16 已定，见文首）**：D1 含 `llm_inference`；D2 `AB_CHAT_EXPERIMENT_ID`；D3 白名单含召回参数（离线 A/B 通过前不启用）；D4 接入 chat 路径。

**待办回写点**：实施后更新 [project-status.md](../project-status.md) §二 LEARN 行与 [README.md](../README.md) 状态表；功能表「🧠 动态学习」「A/B 实验」的「未接入问答链路」限定说明据实调整。

---

## 9. 实施记录（2026-10-16）

三项工作项全部落地，新增开关均灰度默认关；合入零行为变更。

### W-A 单例 + 权重回载（闭合断链②）

- `strategy_manager.py`：新增 `get_strategy_manager()` / `reset_strategy_manager()`（进程内唯一实例）+ `StrategyManager.load_from_store()`（逐策略经 `sample_store.get_strategy_config` 回载 weight/enabled；阈值读 `__decision__` 行 config.threshold）。
- `rag_chain._async_init`：`self.strategy_manager = self.strategy_manager or await get_strategy_manager()`。
- `main.py`：`_periodic_learning_task` 改用单例；lifespan 在 `LEARNING_WEIGHT_RELOAD_ENABLED=true` 时启动回载（失败仅告警）。
- `api/learning.py::trigger_learning`：改用单例。
- `rule_learner.update_threshold_based_on_feedback`：阈值随权重一并持久化至 `__decision__` 行，供回载。

### W-B 短路路径样本补采（闭合断链①）

- `learning_engine.py`：新增 `collect_strategy_results()`（重跑策略投票仅取各策略置信度，LLM 策略为 fast）与 `record_chat_execution()`（结果非空直接使用；为空且 `LEARNING_SAMPLE_COLLECT_BG_ENABLED=true` 时补采）。
- `api/chat.py`：非流式与流式（`save_assistant_message`）均改用 `record_chat_execution`；流式记录移出 DB 会话块，避免补采期间占用连接。

### W-C A/B 分流接入 chat（闭合断链③）

- `config.py`：新增 `ExperimentSettings`（`AB_EXPERIMENT_CHAT_ENABLED` / `AB_CHAT_EXPERIMENT_ID`）。
- `pipeline/state.py`：新增请求级 `_request_ab_config` ContextVar + `set/reset/get_ab_config()`。
- `experiment_manager.py`：新增 `allocate_traffic_for_chat()`（系统级、绕过对象级归属校验，返回 `{variant_id,name,config}`）与 `record_metric_for_chat()`。
- `api/chat.py`：回答前 `_resolve_ab_config()` 解析变体 → 白名单过滤 → `set_ab_config()` 覆盖；`search_mode` 直接透传；回答后记录 `response_time` 指标（fail-soft）；管线消费完毕 `reset`。
- 读点：`decision_pipeline.decide`（门控投票开关 + 策略权重覆盖，经 `should_use_knowledge_base(weights_override=...)`）、`milvus_service._ab_rerank_top_k()`（rerank 候选数），无覆盖时全部回落 settings 默认。

### 验证

- 新增/扩展单测：`test_strategy.py`（+3：load_from_store 回载/缺省、单例同一、fuse 权重覆盖）、`test_learning.py`（+6：补采收集/异常兜底/三种短路分支、A/B 覆盖读点）、`test_experiment.py`（+2：系统级分流返回 config / 未启动与非法 UUID 不分流）。
- `.env.example` 已同步 4 个开关（W-A/W-B/W-C）。
- 后端全量回归：**1042 passed / 105 skipped**（基线 1032 passed / 103 skipped）。

### 端到端验证（2026-10-16，dev 栈三开关全开）

环境：`docker compose -f docker-compose.dev.yml`（rag-dev，源码挂载热重载）启动 dev 栈；`.env.dev` 置三开关为 true；建运行中实验（control 权重 0 / variant 权重 1，variant config `kb_hybrid_rerank_top_k`）并回填 `AB_CHAT_EXPERIMENT_ID`；匿名（dev 默认用户 `default`）经 `/api/chat/messages` 提问。

| 工作项 | 证据 | 结论 |
|---|---|---|
| W-A | 启动日志 `学习权重回载完成（LEARNING_WEIGHT_RELOAD_ENABLED=true）` | ✅ 回载路径生效 |
| W-B | `strategy_executions` 最新行 `strategy_results={"keyword":0.95,"llm_inference":0.85,"context":0.95}`（历史同类行为 `{}`） | ✅ 短路路径补采生效（含 fast `llm_inference`） |
| W-C | `traffic_allocations` 记录 variant 归属；`experiment_metrics` 记录 `response_time`；变体 config `kb_hybrid_rerank_top_k=8` 透传至检索——trace `kb_retrieval.detail.rerank_pairs` 由 3→8（rerank_ms 646→1438 同步印证） | ✅ 分流 + 变体参数覆盖 + 指标回收全通 |

**遗留（dev 环境，非代码问题）**：dev 库 `experiments` 表缺 `owner_id` 列（历史 schema 漂移，早于本次改动），验证前已 `ALTER TABLE` 补齐；`knowledge_bases`/`documents` 归属已临时改为 `default` 以便匿名验证——均为 dev 数据，不影响代码。

