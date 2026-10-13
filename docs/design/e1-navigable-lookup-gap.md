# E-1 导航式 wiki_lookup：能力差距盘点

> 关联：[special-projects-2026-09.md §E.2](special-projects-2026-09.md)（阶段一）、[wiki-navigable-workspace.md](wiki-navigable-workspace.md)（D1-D5 已实施与证伪记录）、[evolution-research-2026-09.md](evolution-research-2026-09.md)
> 日期：2026-10-13 · 状态：盘点完成，实施方案待确认

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
| W1 | 索引页优先入口 | navigator 新增从 PG 直读 `page_type=index` 目录页作第一跳（索引页不进向量库，须 PG/MinIO 直读而非语义检索），Agent 按目录选页；`[[Title]]` 跳转已支持 | 小（1 天） |
| W2 | entity/topic 退主池 A/B | exclude 集合扩展 `("wiki", "wiki_syn")` 的对照实验：wiki_multihop 数据集 + 主检索 hit/recall/mrr 三臂（现状/退池/仅 raw），**证据驱动决定**；退池后 wiki 页仅经 navigator 触达 | 中（A/B 为主） |
| W3 | 导航埋点 + Agent 导航策略 | wiki_lookup 调用埋点进 trace（轮数/取页序列/入口类型）；Agent 工具描述明确"索引页→Related 逐页"使用策略 | 小（1 天） |
| 验收 | 多跳 A/B | wiki_multihop_eval_dataset（recall -0.062 缺口）：E-1 臂（W1-W3 全开）vs 基线臂，Agent 端到端跑；`test_wiki_ab.py` 非退化底线 | — |

## 5. 建议实施顺序

W1（索引页入口，独立价值可先行）→ W3（埋点，为 W2/E-2 提供测量）→ W2（退池 A/B，证据决策）→ 多跳验收。E-2（Query 回流）在 W3 埋点就绪后并行设计回流阈值。
