# Agent 轨迹级评测（AM-1）设计

> 状态：部分实施（**①②③ 已实施 2026-10-20**；④ 在线抽样待办，见 §7）
> 关联：[agent-maturity-gap-analysis.md](agent-maturity-gap-analysis.md)（差距来源）、[agent-ab-evaluation.md](agent-ab-evaluation.md)（现有 A/B 体系）、[e1-navigable-lookup-gap.md](e1-navigable-lookup-gap.md)（E1V 卡点教训）、[project-status.md](../project-status.md) §二「Agent 成熟度差距」AM-1
> 日期：2026-10-20
> 说明：遵循「设计先行 → 确认后实施」。

## 1. 背景与目标

AM-1 的动因是**E1V 的直接教训**：离线 5-slot recall 代理**测不到导航价值**——Agent 的收益在「怎么走」（工具选择、取页序列、步数），而这些**只有轨迹才看得见**。

业界共识（见对标分析）：评测分**离线（回归/发布门禁）+ 在线（生产 trace，评轨迹/效率/合规）**，小样本（~20 例）起步，LLM-judge 打分 0–1 + pass/fail 最稳。

**目标**：让「Agent 走得对不对」可度量、可回归、可门禁。

## 2. 现状盘点（已有 vs 缺）

| 能力 | 现状 | 锚点 |
|---|---|---|
| A/B 跑批 | ✅ `run_agent_ab.py`：live 两臂（`AGENT_ORCHESTRATOR_ENABLED` 控制变量）+ offline KB 回归 + compare 汇总 | `scripts/run_agent_ab.py` |
| 步数/工具统计 | ✅ 从 reasoning 的 `agent_loop` 取 `agent_steps` / `agent_tool_calls` | `run_agent_ab.py#L234-L279` |
| 答案级打分 | ✅ `GenerationEvaluator` LLM-as-judge：`faithfulness` / `relevance`，支持 `strict`/`loose` | `services/evaluation/generation_evaluator.py#L107-L167` |
| 轨迹落库 | ✅ `RequestTrace.tool_calls` / `stages` / `token_usage` / `final_answer` / `total_latency_ms` | `models/request_trace.py#L19-L49`、`trace_collector.py#L44-L83` |
| 评测集 | ✅ `tests/evaluation/agent_eval_dataset*.jsonl`（**有**问题/类别等字段） | `tests/evaluation/` |
| **轨迹级判分** | ❌ **缺**：只统计步数，不判「工具选得对不对 / 步数是否冗余 / 是否覆盖所需信息」 | — |
| **期望标注** | ❌ **缺**：数据集无「期望工具/期望要点」字段 | — |
| **在线评测** | ❌ **缺**：trace 只落库，无抽样跑批/周报/门禁 | — |

## 3. 设计

### 3.1 统一轨迹结构
把两条来源（live 的 reasoning `agent_loop`、trace 的 `tool_calls`）归一为 `Trajectory`：
```
Trajectory(question, steps[], final_answer, total_latency_ms, token_usage)
Step(index, tool, args_digest, ok, observation_digest, latency_ms)
```
（`args_digest/observation_digest` 截断，避免 judge 输入爆长——对齐「按 token 效率优化」。）

### 3.2 数据集扩展（向后兼容）
在 `agent_eval_dataset*.jsonl` 增加**可选**字段（缺失不报错）：
- `expected_tools`：期望调用的工具集合（**弱约束**——只罚「该调未调」，不罚额外调用）
- `expected_answer_points`：参考答案要点（判「信息是否覆盖」）
- `expected_max_steps`：步数上界（可选，默认取 `SEARCH_REACT_MAX_STEPS`）
**首批 20 例**：优先从现有集挑选 + 补标注；跨文档类问题**必须**纳入（E1V 场景）。

### 3.3 轨迹级打分器 `TrajectoryEvaluator`（新增）
位置：`backend/src/services/evaluation/trajectory_evaluator.py`（与 `GenerationEvaluator` 同目录）。
**判分方式（2026-10-20 实现偏差）**：本节原设计为 **LLM-judge**（走 fast llm，守 4B 红線）；实测受 4GB 显存约束、辅助 3B judge 对每一维均不可靠 → **最终四维全部改为确定性规则（去 LLM）**，详见 §7「判分器三轮抽检演化」。对轨迹输出 0–1 分 + `pass/fail` + 理由，四维：
| 维度 | 判什么 | 失败样态 |
|---|---|---|
| 工具选择 | 该用的工具是否用了（对 `expected_tools`） | 漏调/错调 |
| 步数效率 | 有无空转/重复取页（对 `expected_max_steps`） | 冗余步、无进展 |
| 信息覆盖 | 是否覆盖 `expected_answer_points` | 关键点缺失 |
| 合规 | 无越权、无无引用作答 | 越权/幻觉作答 |

输出 JSON（逐维分数 + 总分 + pass）→ 便于门禁与趋势。

### 3.4 离线跑批
扩展 `run_agent_ab.py`：在现有 A/B 产物上**增列** `trajectory_score` / `traj_pass_rate`，并把轨迹判分纳入 compare 报告（**不改变**既有 faithfulness/relevance 口径，只增维度）。

### 3.5 在线评测（后续步）
以 `RequestTrace` 为源，新增抽样跑批入口（脚本）：按时间窗抽 N 条真实 trace → 复原 `Trajectory` → 跑 `TrajectoryEvaluator` → 产出周报（趋势 + 失败样例）。
**说明**：在线部分为**只读旁路**（不改问答链路、不影响线上），先手动/定时脚本，不接 CI 门禁。

### 3.6 开关
`AGENT_TRAJ_EVAL_ENABLED`（默认 **false**，灰度）：仅控制「轨迹判分是否参与 compare 判定门」；不影响回答链路，无需 A/B。

## 4. 验收标准

1. **离线**：跑通 20 例，产出 `trajectory_score` 与 `traj_pass_rate`；`run_agent_ab.py` A/B 报告含新维度。
2. **可回归**：判分靶定（同输入同判分，允许 LLM 抖动记区间）；给 1~2 例**已知坏轨迹**能稳定判 fail（判分器有效性自检）。
3. **成本**：20 例轨迹判分 ≤ 数分钟（fast llm）。
4. **E1V 复用**：Agent 多跳 e2e 可用该判分承载「导航价值」结论。

## 5. 风险与回退

| 风险 | 处置 |
|---|---|
| LLM-judge 不稳定 | 小步先做「pass/fail + 理由」，同输入多次取众数；分数仅作趋势 |
| 标注负担 | `expected_*` 全可选；先只标「期望工具 + 要点」两项 |
| 判分成本 | 走 fast llm；20 例起步，跑批按需 |
| 与既有结论冲突 | 只**增维度**，不改 faithfulness/relevance 既有口径 |
| 回退 | 关 `AGENT_TRAJ_EVAL_ENABLED` 即回现状（一行配置） |

## 6. 建议实施序

**① 轨迹结构 + `TrajectoryEvaluator`（代码 + 单测，mock judge）** → **② 数据集 20 例标注** → **③ `run_agent_ab.py` 接入 + 离线跑通** → **④（后续）在线抽样跑批**。

> 备注：本设计与 AM-2（HITL+护栏）互不阻塞；但**先做 AM-1**——它同时是 E1V 终判（Agent 多跳 e2e）的度量载体。

## 7. 实施记录

### ①②③ 已实施（2026-10-20）

- **① 轨迹结构与判分器**：新增 [trajectory_evaluator.py](../../backend/src/services/evaluation/trajectory_evaluator.py)
  - `TrajectoryStep` / `Trajectory` / `TrajectoryEvalResult`（四维 0-1 + 均分为总分 + pass）
  - 两个还原入口：`build_trajectory_from_tool_calls`（trace 的 `ToolResult.to_dict()`）与 `build_trajectory_from_agent_loop`（reasoning `agent_loop.metadata.tools`，只取 `step=agent_loop` 事件）
  - `TrajectoryEvaluator`：**四维全部确定性规则**（见下方「判分器三轮抽检演化」）；`evaluate`/`evaluate_batch` 保持 async 签名以兼容调用方；无外部依赖、可离线复现；args/observation 摘要截断 **300 字**
  - **单测 16 例全绿**（[test_trajectory_evaluator.py](../../backend/tests/test_trajectory_evaluator.py)）
- **② 数据集标注**：[agent_eval_dataset.jsonl](../../backend/tests/evaluation/agent_eval_dataset.jsonl) **29 例**补 `expected_tools`（弱约束）：`kb_*`→`kb_search`；`web_*`→`web_search`；`hybrid_*`/`multi_*`→`kb_search+web_search`；`tool_01/02`→`calculator`；其余（`fast_*`/`memory_*`/`tool_03/04`）→ 空（不约束）。`expected_points` 原已存在，直接复用为判分要点。
- **③ 离线跑批接入**：[run_agent_ab.py](../../backend/scripts/run_agent_ab.py)
  - `_run_one` 增 `traj_evaluator` 参数与判分块（**失败不阻断单题**，写 `traj_error`）
  - `_arm_metrics` 增 `traj_score` / `traj_pass_rate`
  - `run_live` 按开关创建判分器；**开关关闭时行为与旧版完全一致**（`traj_evaluator=None`）
- **开关**：`AGENT_TRAJ_EVAL_ENABLED`（默认 `false`）+ `AGENT_TRAJ_EVAL_PASS_THRESHOLD`（0.7）；`.env.example` 同步。

### 判分器三轮抽检演化（2026-10-20）

四维原设计为 **LLM-as-judge**（走 fast llm=3B，守 4B 显存红線）。三轮真机抽检证明 **3B judge 对每一维都不可靠**，逐维改为确定性规则：

| 轮次 | 维度 | 3B judge 误判实证 | 处置 |
|---|---|---|---|
| 1 | `tool_selection` | `web_02` 实际调了 `web_search`×2 却说「未使用期望工具」；`hybrid_06` 同类 | 规则化（弱约束：只罚「该调未调」、多调不罚） |
| 2 | `info_coverage` | `hybrid_01/02/03`、`multi_03` **字面已含要点却判「未提及」**（cov=0） | 规则化（要点字符 bigram 覆盖率 ≥0.5） |
| 3 | `compliance` | 把**工具选择**误判为合规问题（`web_04`「未使用 web_search」、`multi_01`「仅用了 kb_search」）；多例**空 reason 却判 0**（web_02/06、hybrid_02、multi_02/03）。收紧 prompt 后仍无效 | 规则化（默认合规；空答→0、命中「无法找到/请先选择知识库」等拒答模式→0.5） |

**结论**：在 4GB 显存约束下（辅助模型只能是 3B），语义类 LLM 判分不可靠；故 `TrajectoryEvaluator` **全维确定性**、不再依赖 LLM——可离线、可复现、进 CI。（更强的 LLM-judge 留待红線解除或引入外部 judge 时再启用。）

**待办**
- **④ 在线抽样跑批**（以 `RequestTrace` 为源，**只读旁路**，不动问答链路）。

### hybrid/multi「未走 web」修复（2026-10-20）

- **根因**：路由层对 hybrid 判 `needs_kb=true & needs_web=true`（`suggested_tools=["web_search"]`），但编排层 [agent.py](../../backend/src/services/pipeline/agent.py) 调 `run_stream` 时**未传 needs_web**；编排器决策 prompt 反而写着「知识库能覆盖的问题不要依赖 web_search」、首步强制工具后缀为「知识库优先 kb_search，否则 web_search」→ Agent 只调 `kb_search` 即作答。
- **定夺**：判定为**真实行为缺陷**（多为「公司 vs 行业/法定标准」对照题，KB 只覆盖公司半边），**保留期望 `[kb_search, web_search]`**（不放宽掩盖）。
- **修复**：新增 `AgentLoopState.needs_web` 与 `run_stream(needs_web=...)`；混合问题时决策 prompt 注入「kb+web 双源」引导、首步强制工具后缀改双源版（[agent_orchestrator.py](../../backend/src/services/agent_orchestrator.py)）；由 `intent_decision(needs_kb && needs_web)` 透传（[agent.py](../../backend/src/services/pipeline/agent.py)）。
- **验证**：`tool_selection` 0.70 → **0.97**（hybrid 5/6、multi 3/3 达 1.0；仅 `hybrid_01` 本轮仍只走 kb）。单测 +3。

### Agent 答案输出污染修复（2026-10-20）

- **现象**：`web_01` 整段为过程叙述（`第 2 步调用 wiki_lookup({...}) … 观察结果：[1] …`）、`web_03` 真实答案尾部带 `下一步操作：调用 wiki_lookup({...})`（2/15）。
- **根因**：① 编排器**纯 Agent 模式的 `direct_answer` 路径未清洗即流式外发**（综合生成分支有清洗，直答分支漏了）；② `OutputSanitizer.looks_like_tool_call` **只识别 JSON 型**工具调用污染，漏掉「叙述型」；③ 3B 模型停止调工具时偶发把 scratchpad 当最终答案。
- **修复**：新增 `_TOOL_NARRATION_PATTERNS` / `looks_like_tool_narration`（`第 N 步` / `观察结果：` / `下一步操作：` / `调用 X({...})`，行首锚定防误伤正文），接入 `sanitize` 行级过滤；`run_stream` 的 `direct_answer` 先 `sanitize` 再外发，清空则回落综合生成或上游 Phase 2 降级（[output_sanitizer.py](../../backend/src/services/output_sanitizer.py)、[agent_orchestrator.py](../../backend/src/services/agent_orchestrator.py)）。单测 +4。
- **验证**：同数据集复跑 **污染答案 0/29**（此前 2/29）；单测全绿；全量 **1070 passed / 104 skipped**。

### 真机基线（2026-10-20/21，dev 栈；live B 臂 29 题）

**运行口径**：在 **backend-dev 容器内**执行 `run_agent_ab.py --mode live --arm B --kb-ids ab_eval_kb`（`AGENT_TRAJ_EVAL_ENABLED=true`）。
> **宿主直跑不可行**：检索链路会在宿主探测 HF 模型（`bge-reranker-base` HEAD，被网络重置并重试 5 次）叠加冷启动 → **90s/题全超时**；容器内服务内网可达、模型已烘焙，正常运行。

**最终基线（确定性判分器 + hybrid 双源 + 输出污染修复）**

| 指标 | 值 |
|---|---|
| 样本 / 错误 | 29 / **0** |
| 已判分（Agent 路径：web 6 + hybrid 6 + multi 3） | **15** |
| 跳过（无工具轨迹：kb 6 + tool 4 + fast 2 + memory 2） | **14** |
| **`traj_score`** | **0.981** |
| **`traj_pass_rate`** | **1.000**（15/15） |
| 各维均值 | `tool_selection` 0.97 / `step_efficiency` 1.00 / `info_coverage` 0.96 / `compliance` 1.00 |
| 污染答案 | **0/29** |

**判分器演化对拍（同一数据集、不同轮次）**

| 判分器版本 | `traj_score` | `traj_pass_rate` |
|---|---|---|
| LLM 三维 + LLM info（首版） | 0.787 | 0.60 |
| info_coverage 规则化 | 0.747 | 0.533 |
| + compliance prompt 收紧（仍 3B 判） | 0.750 | 0.667 |
| 全维确定性判分器 | 0.872 | 0.933 |
| ＋ hybrid/multi 双源修复 | 0.883 | 0.867 |
| **＋ 输出污染修复（最终）** | **0.981** | **1.000** |

> 说明：各轮**答案集随每次 live 跑批变化**（LLM/联网结果非确定），故非严格同题对拍。趋势：确定性判分器消除了 3B judge 的**假失败**；双源修复把混合类工具选择拉回 1.0；污染修复消除了「过程叙述当答案」导致的合规与覆盖失分。**注**：最后一轮恰好答案质量也较好（`info_coverage` 0.72→0.96），故总分跃升含运气成分，不宜按 1.0 当作稳定水位。

**基线读出的真实轨迹问题**
- **【已修复】`hybrid_*`/`multi_*` 的 `tool_selection` 0.5 → 1.0**：根因为编排器 kb-first 偏置未采纳路由 hybrid 意图（见「hybrid/multi 修复」）。
- **【残留】`hybrid_01` 仍 `ts=0.5`**：双源引导后仍只调 `kb_search`——提示引导不等于确定性保证，若需硬保证应改为「首步多工具强约束」。
- **【已修复】`web_01`/`web_03` 输出污染**：答案泄漏工具调用叙述（见「输出污染修复」）。

**待办**
- **答案级判分器复核**（`GenerationEvaluator` 亦为 3B：`faithfulness` 0.34 / `relevance` 0.12 偏低，疑同类不可靠）——待确认后再动。
- **④ 在线抽样跑批**（以 `RequestTrace` 为源，只读旁路）。
- **`hybrid_01` 双源硬保证**（可选：首步强制 kb+web 并发调用）。
