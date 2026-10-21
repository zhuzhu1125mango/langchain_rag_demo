# E-1 导航式 wiki_lookup：能力差距盘点

> 关联：[special-projects-2026-09.md §E.2](special-projects-2026-09.md)（阶段一）、[wiki-navigable-workspace.md](wiki-navigable-workspace.md)（D1-D5 已实施与证伪记录）、[evolution-research-2026-09.md](evolution-research-2026-09.md)
> 状态：**部分实施**（W1 索引页优先入口 / W2 entity-topic 退主池 / W3 导航埋点均已实施 2026-10-15，见 §6；仅多跳端到端验收 E1V 待办；遵循「设计先行 → 确认后实施」）
> 日期：2026-10-13

## 1. 关键前提修正

阶段一启动前的假设是"E-1 从零建导航能力"。盘点结论：**D1-D4 已于 2026-09-23 落地 E-1 的主体能力**（代码+16 单测保留至今，全部灰度默认关），E-1 实际是"存量能力激活 + 三处差距补齐"。

## 2. 能力矩阵（E-1 目标 vs 现状）

| E-1 目标能力 | 现状 | 判定 | 锚点 |
|---|---|---|---|
| 按页取读 + Related 透出（progressive disclosure 载体） | `wiki_navigator.py` get_page_by_title：PG 标题定位 → Milvus 按 chunk_index 拼页 → 4000 字截断 → Related `[[标题]]` 引导续读 | **已有** | D1 落地，7 单测 |
| 双路语义入口检索（wiki + wiki_syn 各 top4） | search_entries 并行召回 + 反查 PG 附加 page_links | **已有** | wiki_navigator.py#57-117 |
| Related 链接结构化存储 | PG `wiki_pages.links` JSONB（正文 `[[Title]]` 提取去重）——**D2 失败教训要求的"链接元数据与正文分离"载体已存在** | **已有** | models/wiki_page.py#38 |
| 索引页目录定位（先读索引再选页） | 无。索引页 `page_type=index` 已生成且每次编译后维护，但**不进向量库**（仅 MinIO 正文 + PG 行）→ 语义入口检索无法命中它；入口也无"先读目录"策略 | **缺失** ← 差距① | wiki_compiler.py#757-812 |
| 编译页仅入口定位、不与原始 chunk 同池 | wiki_syn 已排除主池（kb_retrieval_service 默认 exclude）；**entity/topic 页仍与 raw 同池竞争** | **部分** ← 差距② | milvus source_kind 机制 |
| Agent 多轮取页的编排形态 | 有界 plan-execute 循环已上线（Agent 批次），wiki_lookup 可多轮调用 | **已有** | pipeline/agent.py |
| 导航收益的测量手段 | 无 wiki_lookup 多轮调用埋点（D 臂当年失败正是"离线 5-slot 代理测不到导航价值"） | **缺失** ← 差距③ | 重启触发条件之一 |
| E-2 依赖：synthesis 机制 + 记忆写入通道 | D3 全套（默认灰度关）+ L1-a memory 写入（AGENT_MEMORY_ENABLED=false） | **已有（灰度关）** | E-2 时开 |

## 3. 必须吸收的历史教训（D2/D5 证伪，2026-09-23）

| 教训 | 对 E-1 的约束 |
|---|---|
| 互链 prompt 内联文本稀释检索（B 臂 0.938→0.812），5-slot 内邻居挤占 golden 文档 | 互链读取走 links JSONB 结构化元数据，**严禁**往页面正文塞内联链接文本 |
| 5-slot 被动检索原理上测不到导航价值 | 验收以 **Agent 多轮端到端 A/B**（真实取页）为主，离线 recall 仅作不退化底线 |
| 混入主检索的动作必须 A/B | entity/topic 退主池同样先 A/B 后定（方向有据：D5 证明混入负收益） |
| 重启触发条件 | ①真实 trace 跨文档问题占比可观；②多轮导航有真实测量手段——差距③正是为此 |

## 4. E-1 实施建议（三差距 → 三工作项）

| # | 工作项 | 内容 | 规模 |
|---|---|---|---|
| W1 ✅ | 索引页优先入口 | navigator 新增从 PG 直读 `page_type=index` 目录页作第一跳（索引页不进向量库，须 PG/MinIO 直读而非语义检索），Agent 按目录选页；`[[Title]]` 跳转已支持 | 小（1 天） |
| W2 ✅ | entity/topic 退主池 A/B | exclude 集合扩展 `("wiki", "wiki_syn")` 的对照实验：wiki_multihop 数据集 + 主检索 hit/recall/mrr 三臂（现状/退池/仅 raw），**证据驱动决定**；退池后 wiki 页仅经 navigator 触达 | 中（A/B 为主） |
| W3 ✅ | 导航埋点 + Agent 导航策略 | wiki_lookup 调用埋点进 trace（轮数/取页序列/入口类型）；Agent 工具描述明确"索引页→Related 逐页"使用策略 | 小（1 天） |
| 验收 | 多跳 A/B | wiki_multihop_eval_dataset（recall -0.062 缺口）：E-1 臂（W1-W3 全开）vs 基线臂，Agent 端到端跑；`test_wiki_ab.py` 非退化底线 | — |

## 5. 建议实施顺序

W1（索引页入口，独立价值可先行）→ W3（埋点，为 W2/E-2 提供测量）→ W2（退池 A/B，证据决策）→ 多跳验收。E-2（Query 回流）在 W3 埋点就绪后并行设计回流阈值。

---

## 6. 实施记录

### W1 索引页优先入口（2026-10-15 已实施）

**开关**：新增 `WIKI_NAV_INDEX_FIRST_ENABLED`（`WikiCompileSettings`，灰度默认 **false**；`.env.example` 同步）。属工具内部导航、不混入主检索，故不触发「混入主检索必先 A/B」红线；开关供 E1V 多跳 A/B 切换对照臂。

**改动**：
- `wiki_navigator.py`：新增 `get_index_page(kb_ids)`——PG 直读 `page_type=index` 目录页（`status=active`），正文经 **MinIO 直读**（`_read_minio_content`，索引页不进向量库）；`_read_page_content` 由「按 id 走向量库」重构为「按页型分流」（index → MinIO；其余 → 向量库分块拼接，无分块时回退 MinIO），`get_page_by_title` 同步支持按标题取读索引页。
- `wiki_lookup_tool.py`：`_entry_search` 在开关开启时先返回索引页目录作**第一跳**（`[索引页] 标题` 块 + source），再附语义命中切片；无语义命中但有索引页时返回目录（不再回退「暂无相关」）。

**验证**：`test_wiki_navigator.py` 新增 4 用例（索引页 MinIO 直读 / 无索引返回 None / 按标题取读索引页 / 工具入口索引优先 + 无命中回退 + 开关关闭保持旧行为）；后端全量 **1024 passed / 103 skipped**。

**遗留**：开关默认关，需在 `.env.dev` 设 `WIKI_NAV_INDEX_FIRST_ENABLED=true` 方可在 dev 观察到「索引页先行」；W3 埋点（入口类型统计）落地后可对 E1V 多跳 A/B 做证据评估。

### W3 导航埋点 + Agent 导航策略（2026-10-15 已实施）

**埋点**：新增请求级缓冲 `_request_wiki_nav`（[state.py](../../backend/src/services/pipeline/state.py)；ContextVar 承载**可变列表**——`asyncio.gather` 子任务共享同一引用，工具侧 append 对父任务可见）+ `record_wiki_nav`（工具侧写入）与 `summarize_wiki_nav`（汇总：轮数 / 入口类型序列 / 取页序列 / 索引入口轮数）。管线 `_pipeline` 起始建缓冲，`_finalize_side_effects` 把汇总写入 trace 的 `stages` 明细（`name=wiki_nav`）——**刻意不新增 trace.data 键**：`RequestTrace` 是固定列模型，未知键会使 `RequestTrace(**data)` 抛 TypeError 致落库失败。
- `wiki_lookup_tool.py`：入口检索记 `{mode:"entry", index_used}`、按页取读记 `{mode:"page", target}`。

**Agent 策略**：工具 `description` 明确「先读【索引页】目录 → 沿目录/Related 逐页取读；跨文档问题多走几步」。

**验证**：`test_wiki_navigator.py` 新增 3 用例（汇总 / 工具写入缓冲 / 无缓冲 no-op）；后端全量 **1027 passed / 103 skipped**。

**顺带发现（已修复，见文末「TRACE 修复」）**：`RequestTrace` 固定列不含 `context_rewrite` / `semantic_cache_hit` / `verification` 三键，而 `pipeline/stages.py` 会写入 → 这三类路径（问题改写 / 语义缓存命中 / 非流式 web 校验）的 trace 落库静默失败。

### W2 entity/topic 退主池 A/B（2026-10-15 已实施）

**机制**：主检索默认排除集合由 `KB_RETRIEVAL_EXCLUDE_SOURCE_KINDS` 驱动（[config.py](../../backend/src/config.py) `ProcessingSettings`，逗号分隔；[kb_retrieval_service.py](../../backend/src/services/kb_retrieval_service.py) 的 `_configured_exclude_kinds` 解析，显式传参可覆盖）。现状 `"wiki_syn"`（entity/topic 与 raw 同池）vs 退池 `"wiki,wiki_syn"`（编译页全退主池，仅经 wiki_lookup 导航触达）。

**三臂口径**：现状 = 池内 raw+wiki；退池 = 仅 raw；「仅 raw」与退池在主检索口径下**等价**，故退化为两臂比较（判定函数 `run_wiki_multihop_ab.py::w2_recommend`，报告新增 `w2_arms` 段）。

**证据（wiki_multihop 数据集 8 题 × 12 篇语料，BM25 离线代理）**：仅 raw recall@5=**0.938** vs raw+wiki **0.812**（hit/mrr 均 1.0 持平）→ 编译页混入主池稀释跨文档召回 **−0.125** → **采纳退池**（默认 `wiki,wiki_syn`）。

**验证**：`test_multi_query_retrieval.py` 新增 3 用例（配置解析 / 缺省透传 / 显式覆盖）；后端全量 **1032 passed / 103 skipped**。

**回退**：设 `KB_RETRIEVAL_EXCLUDE_SOURCE_KINDS=wiki_syn` 即回退现状（一行配置）。

**复跑说明**：2026-10-15 曾发起一次复跑以复核（harness 默认以 `OLLAMA_MODEL_NAME=qwen3:4b` 思考版逐页重编译全量语料），运行 ~79 分钟仍未产出、遂**中止**。**W2 证据基线仍为 2026-09-23 报告（0.938 vs 0.812）**，结论不变。E1V 复跑前应给 harness 加「编译页缓存」并以 `python -u` 运行，避免每次 1~2 小时盲等。

### TRACE 修复（2026-10-15，随 W2/W3 顺带）

`RequestTrace` 为固定列模型，而 `save_async` 直接 `RequestTrace(**self.data)`——data 含无对应列的键（`semantic_cache_hit` / `verification` / 冗余 `context_rewrite`）时抛 TypeError，被 best-effort 吞掉致**整条 trace 落库失败**（问题改写 / 语义缓存命中 / 非流式 web 校验三类路径）。修复：`TraceCollector._to_record_payload` 按模型列过滤（未知键丢弃并告警，不再波及整条记录）+ 移除 `context_rewrite` 冗余顶层写（该值已随 `intent_decision.to_dict()` 持久化）。验证：`test_trace_ownership.py` 新增 2 用例。

### E1V 多跳 A/B 复跑（2026-10-19，离线 BM25 代理）

AB-CACHE 落地后复跑 [run_wiki_multihop_ab.py](../../backend/scripts/run_wiki_multihop_ab.py)（语料 12 篇 / 8 问，`python -u`，qwen3:4b 编译）。本轮编译成功产出 **11 页**（上次尝试因 Ollama 连接失败仅 2 页）。

| 臂 | hit@5 | mrr@5 | recall@5 |
|---|---|---|---|
| A 仅原始 chunk | 1.000 | 1.000 | **0.938** |
| B 原始 chunk + wiki | 1.000 | 1.000 | 0.875 |
| C 仅 wiki 页（诊断） | 0.375 | 0.375 | 0.188 |
| D 导航式（入口+链接） | 0.375 | 0.375 | 0.188 |

**结论**
- **W2 退池复现**：A(0.938) > B(0.875)，Δrecall=+0.062 → 编译页混入主池仍稀释跨文档召回，**退池结论不变**（与 2026-09-23 基线方向一致：0.938 vs 0.812）。
- **混入无增益 / 缺口未收复**：B 相对 A recall −0.062。
- **wiki 页自身跨文档召回低**（C=0.188）：编译页未覆盖跨文档关联 → 轻量 GraphRAG 立项依据（同前）。
- **⚠️ D 臂不可用 —— 经查属既定现状，非新缺陷**：本轮 **[[互链]] 0 条** → D≡C。根因：[wiki_compiler.py `_generate_page`](../../backend/src/services/wiki_compiler.py#L326-L363) 的页面生成 prompt 已在 **2026-09-23 D2/D5 证伪后回退**（不再要求 `[[Title]]`），`WIKI_LINK_EXPANSION` 亦默认 `false` → **当前默认编译不产图边**，navigator 读的 `links` JSONB 为空 → 「Related 逐页」无链接可循。日志的 `Wiki 候选抽取失败: Extra data` 系 `_extract_candidates()` 的 LLM JSON 解析告警，与无桥**无因果**（初判有误，已更正）。
- 故 D 臂/导航价值在**默认编译下离线不可测**；按红线，导航价值最终以 **Agent 多轮端到端 A/B** 为准（离线 5-slot 仅非退化底线）。

**破局点（建议）**：W2 退池后 wiki 页**已不在主检索池**，"链接文本内联正文→稀释主检索"的证伪前提（D2/D5 的负收益来源）**已不再适用主池**。故可重估 prompt 注记中所谓"正规建法"——**仅写 `links` JSONB 元数据、不内联正文**（页面正文不含 `[[..]]`），据此重建图边后再测 D 臂与 Agent 导航。此项属对既定证伪结论的**重新评审**，须设计先行、确认后实施。

### E1V 复跑（2026-10-20，编译覆盖修复后）—— 结论修正

排查中发现**真瓶颈是编译覆盖**：12 篇语料中 8 篇因 `_parse_json_dict` 兜底正则（贪婪 `\{.*\}`）解析 LLM 输出失败 → **零页面**（含 `reimbursement`/`attendance` 等多跳枢纽档）。修复（改 `raw_decode`）后冷重编译：**12/12 篇 → 34 页**。

| 项 | 修复前（10-19） | 修复后（10-20） |
|---|---|---|
| 编译覆盖 | 4/12 篇（11 页） | **12/12 篇（34 页）** |
| 图边（L1 元数据） | — | **47 条（跨档 13）** |
| A 仅 raw | 0.938 | 0.938 |
| B raw+wiki | 0.875 | 0.875 |
| **C 仅 wiki** | **0.188** | **0.938** |
| D 导航式 | 0.188 | 0.688 |

**修正后的结论**
- **"跨文档缺口"主要是编译覆盖问题**：修复后 **C 臂 = 0.938**，与 raw 基线**持平** → 不依赖导航即可达基线。
- **L1 跨档建边成立**：13 条跨档边恰对齐多跳金标对（如 `remote_work↔attendance`）→ 此前"跨档 0"系覆盖混淆。
- **退池结论不变**（A 0.938 > B 0.875）。
- **导航（D）离线仍为负**（0.688），但 harness 的 D 排序把邻居页置于其余入口页之前（保守偏置）→ **导航价值仍须 Agent 多轮 e2e 判定**（红线）。

详见 [wiki-link-metadata-edges.md §7](wiki-link-metadata-edges.md)。
