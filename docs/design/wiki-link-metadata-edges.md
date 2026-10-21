# Wiki 图边「仅元数据建边」重新评审与设计

> 状态：部分实施（**L1 已实施 + 编译覆盖修复 + 离线验证完成，2026-10-20**；L2 暂缓，导航须 Agent e2e，见 §7）
> 关联：[e1-navigable-lookup-gap.md](e1-navigable-lookup-gap.md)（E1V）、[wiki-navigable-workspace.md](wiki-navigable-workspace.md)（§10 D2/D5 证伪）、[project-status.md](../project-status.md)（E1V 行）
> 日期：2026-10-19
> 说明：遵循「设计先行 → 确认后实施」。本设计**重新评审** D2/D5 的证伪结论，仅提出「编译期建图边元数据」方案，不改正文、不改主检索默认。

## 1. 背景与触发

E1V 离线多跳 A/B 复跑（2026-10-19）显示 **D 臂（导航式）不可用**：本轮编译产出 **0 条 `[[互链]]`** → D≡C（recall@5 均 0.188）。经排查，这是**既定现状而非新缺陷**：

| 环节 | 现状 | 锚点 |
|---|---|---|
| 正文互链 prompt | **2026-09-23 D2/D5 证伪后已回退**，页面生成不再要求 `[[Title]]` | [wiki_compiler.py#L326-L363](../../backend/src/services/wiki_compiler.py#L326-L363) |
| 交叉链接扩展 | `WIKI_LINK_EXPANSION` 默认 `false` | [config.py](../../backend/src/config.py) |
| links 来源 | **仅**由正文 `[[Title]]` 正则提取并校验目标页存在后写 `wiki_pages.links` | [wiki_compiler.py#L495-L519](../../backend/src/services/wiki_compiler.py#L495-L519)、`extract_links` [#L865](../../backend/src/services/wiki_compiler.py#L865) |
| 导航消费 | navigator 读 `wiki_pages.links` 作 Related（`MAX_RELATED=8`） | [wiki_navigator.py#L82-L117](../../backend/src/services/wiki_navigator.py#L82-L117)、[#L168-L210](../../backend/src/services/wiki_navigator.py#L168-L210) |

**推论**：正文不含 `[[..]]` ⇒ `links` 为空 ⇒ navigator 的「Related 逐页」**无链接可循** ⇒ E-1 的导航价值在默认编译下**离线不可测**（须 Agent 多轮 e2e 才有意义）。

## 2. 前提变化：为何现在可重估 D2/D5

D2/D5 的证伪理由是「**互链文本内联进页面正文 → 稀释主检索**」（B 臂 0.938→0.812）。但两点已使该理由的作用面消失：

1. **W2 退池已落地**：主检索默认排除 `wiki,wiki_syn`，编译页**已不在主检索池** → 正文是否含链接文本，**不再影响主检索**。
2. **红线本就要求分离**：项目红线明示「互链**严禁内联正文**，走 PG `links` JSONB」。D2/D5 的负收益正是**违背该约束**（内联正文）的产物；遵循约束的「元数据建边」从未被单独验证过。

故：以「**仅写 `links` 元数据、正文零改动**」重建图边，既满足红线、又不触碰已被证伪的稀释路径——具备重新评审条件。

## 3. 设计

### 3.1 目标
在**不改页面正文**的前提下，为每页生成 `wiki_pages.links`（JSONB）图边，供 navigator 的 Related 与 Agent 多轮导航使用。

### 3.2 开关
新增 `WIKI_LINK_METADATA_ENABLED`（`WikiCompileSettings`，**灰度默认 false**，`.env.example` 同步）。与既有 `WIKI_LINK_EXPANSION` **解耦**：后者是**查询期**把 links 内容追加为扩展块（会进上下文），本开关只管**编译期建边**。本设计**不改** `WIKI_LINK_EXPANSION` 默认值。

### 3.3 建边策略（分层，成本递增，可独立启用）
| 层 | 做法 | 依赖 | 说明 |
|---|---|---|---|
| **L1 确定性候选** | 目标页标题 / 实体（如 jieba `ns` 等）在**本页来源材料或正文**中出现 → 建候选边 | 无 LLM | 零成本、可复现；复用既有 `normalize_title` 与目标页存在校验 |
| **L2（可选）fast-LLM 筛选** | 输入本页摘要 + 既有页标题清单，输出**严格 JSON** 标题数组 | **fast llm**（红线：辅助任务禁 4B） | 语义桥接更准；每页 1 次调用；解析失败即降级（保留 L1 结果） |

落库口径与现状一致：去重、排除自引用、仅保留 `status=active` 的目标页、数量设上限。

### 3.4 落点
在 `compile_pages` 的 upsert 之后，新增/替换建边步骤（`_fill_links` 的元数据分支）：
- 开关关 → 保持现状行为（正文 `[[..]]` 提取，通常为空）；
- 开关开 → 按 §3.3 建边写入 `links`，**正文一字不改**。

## 4. 验收

1. **离线（harness）**：开关开启后 D 臂 `links>0`、导航图非退化；**主检索三臂 A/B 不变**（图边不在正文 → 不影响任何检索池）。
2. **Agent 多轮端到端 A/B**（**红线主验收**）：E-1 臂（索引页入口 + 图边导航）vs 基线臂，比对跨文档问题 recall 与取页序列（W3 埋点 `wiki_nav`）。
3. **非退化底线**：`test_wiki_ab.py` 不回退。
4. **正文零改动断言**：建边前后页面正文哈希一致。

## 5. 风险与回退

| 风险 | 处置 |
|---|---|
| LLM 噪声边（幻觉标题） | 目标页存在校验 + 数量上限；L2 解析失败降级 L1 |
| 编译成本增加 | L1 零 LLM；L2 仅 fast llm/页，且受开关控制 |
| 与 D2/D5 结论冲突 | 明确边界：本设计**不内联正文**；若 Agent e2e 仍无收益，则如实记录证伪 |
| 回退 | 关闭 `WIKI_LINK_METADATA_ENABLED` 即回现状（一行配置） |

## 6. 建议实施序

设计确认 → **L1 确定性建边**（代码 + 单测）→ 离线 D 臂复跑（验证 `links>0` 且主检索不变）→ 视结果决定是否上 **L2** → **Agent 多轮 e2e A/B**（导航价值终判）→ 回写 [e1-navigable-lookup-gap.md](e1-navigable-lookup-gap.md) 与台账。

> 备注：本设计与 [e1-navigable-lookup-gap.md](e1-navigable-lookup-gap.md) §6「E1V 破局点」互为表里；E-1 的 W1（索引页入口）已在索引维度提供第一跳，本设计补齐**图边维度**的第二跳到第 N 跳。

## 7. 实施记录

### L1 确定性建边（2026-10-20 已实施；离线验证暴露结构瓶颈）

**改动**
- [config.py](../../backend/src/config.py)：新增 `WIKI_LINK_METADATA_ENABLED`（`WikiCompileSettings`，默认 `false`）；[.env.example](../../.env.example) 同步。
- [wiki_compiler.py](../../backend/src/services/wiki_compiler.py)：新增 `derive_links_from_titles()`（按「其他页标题出现在本页正文」建边，**正文零改写**）；`_fill_links` 按开关分流（开 → 元数据建边；关 → 既有 `[[..]]` 提取）。
- [run_wiki_multihop_ab.py](../../backend/scripts/run_wiki_multihop_ab.py)：`build_link_graph` 与链接图计数同开关对齐（离线 D 臂与生产 `links` 同源）。
- 单测：[test_wiki_compiler.py](../../backend/tests/test_wiki_compiler.py) 新增 5 例（4 例纯函数 + 1 例落库元数据模式）。

**验证（离线，复用 11 页编译缓存，秒级）**
- 开关开启：图边 **15 条**、**正文零改动**；主检索三臂**不变**（A 0.938 / B 0.875 / C 0.188）。
- **D 臂未改善（0.188 = C）**：15 条边**全部为同文档内**（`cross_doc=0`）。
- **根因**：wiki 页按**来源文档**逐篇编译，页面正文只提及本档实体 → L1 的「标题出现」只能产出**档内边**；而多跳要跨文档，**档内边无法桥接** → doc 级 recall 不变。
- **结论**：L1 能让 navigator 的 **Related 非空（档内导航可用）**，但**结构上不能闭合跨文档缺口**；继续推进须 **L2 语义建边（跨档）**——待决策。

**待办**
- L2（fast-LLM 语义建边）决策与实施；Agent 多轮 e2e A/B（红线主验收）。

### 补充发现：编译覆盖仅 4/12 篇（2026-10-20，随 L1 验证排查）

同次排查发现**上面「跨档边 0」的结论受混淆**，真因是**编译覆盖不足**：

- 12 篇语料中 **8 篇候选抽取失败**（`attendance` / `reimbursement` / `remote_work` / `leave_policy` / `work_hours` / `expense_standard` / `customer_reception` / `info_security`），**零页面**——恰含多跳金标里的**枢纽档**（`reimbursement` 命中 3 题、`attendance` 命中 3 题）。
- 只有 4 篇（`bonus_policy`/`meeting_policy`/`it_equipment`/`training_policy`）编出页 → 11 页。
- **根因**：`_parse_json_dict` 的兜底正则 `_JSON_BLOCK_RE = r"\{.*\}"`（DOTALL 贪婪）会把「首个 `{` → 最后一个 `}`」（含尾随多余文本）整段喂 `json.loads` → `Extra data` 报错 → **整个文档零页面**。
- **修复**：改用 `json.JSONDecoder().raw_decode` 只消费**一个** JSON 值、忽略其后内容（单测 `test_parse_json_variants` 增 2 例；`test_wiki_compiler.py` 73 passed）。

> **推论**：L1 的「15 条边全为档内」**并非 L1 设计缺陷**——当只有 4 个档的页存在时，标题匹配**只能命中同档页**。修复覆盖后（预期 12 篇全编出），跨档边可能自然出现（如「培训经费」页正文提及「报销」→ 若存在 `reimbursement` 档的「报销」页即形成跨档边）。**L1/L2 决策应以修复后的冷重编译结果为准**。

### 覆盖修复后冷重编译复跑（2026-10-20）

`_parse_json_dict` 修复后以 `--no-cache` 全量重编译（**34 页，12/12 篇全编出**），四臂 A/B 对比：

| 项 | 修复前 | 修复后 |
|---|---|---|
| 编译覆盖 | 4/12 篇（11 页） | **12/12 篇（34 页）** |
| 图边 | 15 条（跨档 **0**） | **47 条（跨档 13）** |
| A 仅 raw chunk | 0.938 | 0.938 |
| B raw + wiki | 0.875 | 0.875 |
| **C 仅 wiki 页** | **0.188** | **0.938** |
| D 导航式 | 0.188 | 0.688 |

**结论**
1. **真正的瓶颈是编译覆盖**：修复后 **C 臂 0.188 → 0.938**，与 raw 基线**持平** → 多跳"缺口"主要由**编译覆盖不足**造成，**而非导航能力缺失**（此前"GraphRAG 立项依据"的判断随之弱化）。
2. **L1 跨档建边成立**：47 条边含 **13 条跨档**，且恰好落在多跳金标对上（`remote_work↔attendance`、`leave_policy↔remote_work`、`reimbursement↔leave_policy`）——此前"跨档 0"确系覆盖混淆所致。
3. **退池结论不变**：A(0.938) > B(0.875)，Δ=+0.062。
4. **D 臂离线代理仍为负**（0.688 < C 0.938）：注意 harness 的 D 排序把**邻居页排在其余入口页之前**（=2 入口 + 邻居，截断 top5），会**挤掉优质入口页**——属**代理口径的保守偏置**，不宜据此判导航证伪；导航真实价值仍须 **Agent e2e** 判定（红线）。

**L2 评估**：L1 已产出 13 条跨档边（且对齐金标），**L2 语义建边暂无明显必要**；下一步直接 **Agent 多轮 e2e** 验收导航价值。

**观察（待核）**：本轮出现**同名跨档页未合并**（如 `OA系统` 分别在 `leave_policy` / `reimbursement` / `remote_work` 各生成 1 页，共 3 页）→ 图边按「首个同名页」命中，存在**解析歧义**。是否应跨档**合并同名页**（编译器的 `_match_pages` 精确标题命中未生效）需另议，可能同时影响导航与去重。
