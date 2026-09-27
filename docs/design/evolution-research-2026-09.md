# 演进方向调研：从 RAG 问答到自进化知识运行时（2026-09-23）

> 状态：草稿（待确认后按"设计先行 → 确认后实施"执行）
> 背景：owner 提出——本想借鉴 Claude Code 的 Wiki 模式但不确定是否合适；希望项目不再只是 RAG，向更全面、跟随主流或有创意的方向演进。
> 方法：Web 实际调研（Claude Code wiki 生态、DeepWiki、agentic RAG / context engineering / agent memory 2026 主流格局），结合本项目 A/B 实证与代码现状。

---

## 1. 「Claude Code Wiki 模式」考据：它到底是什么

调研发现这不是单一产品，而是 2025-2026 年成型的**一个范式 + 一圈工具生态**：

### 1.1 范式本体（Karpathy Pattern / LLM Wiki）

Andrej Karpathy 2026-04 提出的 LLM Knowledge Base 模式（社区称 Karpathy Pattern），代表实践是 vanja.io《The Knowledge Base That Builds Itself》。核心结构：

- **目录约定**：`raw/`（不可变原始来源）+ `wiki/`（sources/entities/concepts/synthesis 四类页）+ `index.md` + `log.md`（审计日志）
- **页面格式**：YAML frontmatter + TLDR + 正文 + Related 交叉链接 + Sources 回链
- **三个工作流**：
  - **Ingest**：读 raw → 建 sources 摘要页 → 更新实体/概念页 → 补交叉链接 → 更新索引与日志
  - **Query**：读 index 找相关页 → 逐页阅读 → 综合 → **有价值的答案沉淀为新 synthesis 页**
  - **Lint**：查矛盾、标记过时论断、孤儿页、缺失概念

**关键洞察：查询路径是 Agent 主动导航**（读索引 → 顺 wikilink 逐页读 → 综合），**不是向量检索**。"重认知工作在入库时做一次，查询在策展过的知识上进行"。实测规模约 100 篇来源 / 数百页内可用（索引本身会超上下文，社区解法是给 wiki 目录加一层语义检索做入口定位）。

### 1.2 工具生态

| 工具 | 形态 | 要点 |
|---|---|---|
| **DeepWiki**（Cognition） | 托管服务 | GitHub 仓库 → 交互式 wiki；tree-sitter 建代码图；Ask 分 Fast / Deep Research 两档；**官方 MCP server** 供 Cursor/Claude Code 查询；已索引 5 万+ 仓库 |
| **autowiki** | Claude Code 插件 | 探索代码库生成 `wiki/`（concepts/guides/reference + wikilinks），增量更新，`npx autowiki` 本地建站 + llms.txt |
| **claude-code-wiki** | CLI | Code 模式（仓库文档）+ Personal 模式（个人大脑，接 Gmail/Notion/Web 连接器），输出 Open Knowledge Format |
| **llmwiki** | CLI | 把 Claude Code 会话记录自动入库为 wiki，/wiki-sync /wiki-query /wiki-lint 斜杠命令 |

### 1.3 与本项目 LLM-Wiki 编译层的对照

本项目的 wiki 编译层（蒸馏页 / 交叉链接 / 一致性 lint / 矛盾检查 / 覆盖先验）**明显就是这个范式的检索化移植**——直觉方向是对的，且已被主流验证。但有一处关键偏差：

| | Claude Code Wiki 模式 | 本项目现状 |
|---|---|---|
| wiki 定位 | **Agent 可导航的工作空间**（一等公民） | 检索语料的补充（编译页混入 BM25/向量） |
| 查询路径 | Agent 读索引 → 顺链接逐页读 → 综合 | 被动切片进混合检索，与原始 chunk 同池竞争 |
| 知识回流 | Query 产出沉淀为 synthesis 页（自进化） | 无（答案不回流） |
| 跨文档桥接 | Agent 顺 wikilink 跨页行走（天然解决） | 依赖混入召回（A/B 证实无效，recall -0.062） |

**结论：合适，但要"借神不借形"。** 2026-09-23 的多跳 A/B 恰好实证了"形"的偏差——把导航型知识库降维成检索语料，桥接能力就丢了。正确嫁接方式见 §3 阶段一。

---

## 2. 2026 主流格局：RAG 正在变成什么

综合 Gartner、LangChain/LlamaIndex 路线、agent memory 生态的公开材料：

1. **RAG → Context Engine（知识运行时）**：检索+验证+访问控制+审计成为一体化基础设施，"构建 RAG 管线"让位于"配置 context engine"（预计 2027-2028 成为主流形态）。Gartner：2026 年底 40% 企业应用将集成 AI agent。
2. **Agentic RAG**：检索权交给 agent（CRAG / Self-RAG / 多步规划 / 自评重试），优化目标从"管线精度"变为"agent 导航知识空间的能力"。Progressive Disclosure（渐进式加载、按需取用）成为组织级知识分发的标准模式。
3. **Graph + Memory 融合**：Mem0 / Zep / Letta 等已让"向量+图+时序"混合记忆成为生产共识（MAGMA 框架，2026-01）；纯向量被公认不足以支撑长程 agent。VentureBetty 2026 判断：agentic 记忆的使用量将超过传统 RAG。
4. **MCP 成为分发标准**：Linux 基金会标准化；DeepWiki 等均以 MCP server 形态把知识能力输出给其他 agent——知识库从"应用"变"基础设施"的通道。
5. **知识治理是缺失层**：版本化、来源溯源（provenance）、permission-aware retrieval 被称为"agentic 企业架构中缺失的一层"——恰是企业私有化部署的差异化机会。

**对本项目的定位启示**：本项目已有的东西——决策管线（自适应检索）、评估闭环、Trace、审计日志、owner 隔离、混合检索、Wiki 编译、Agent 编排——**拼起来已经是一个"私有化 context engine"的雏形**，而不是"一个 RAG demo"。差的不是能力，是定位叙事和最后几块积木（回流、MCP、导航、治理收口）。

---

## 3. 建议演进路线（四阶段，按依赖排序）

### 阶段一：借神——Wiki 从"语料"升级为"Agent 可导航工作空间"（借 Karpathy 神髓）

1. **升级 `wiki_lookup` 工具为导航式**：输入问题 → 读 wiki 索引（或语义入口检索）→ 顺 Related 链接逐页取读（progressive disclosure）→ 返回综合上下文。编译页继续保留向量/BM25 索引，但只做"入口定位"，不再与原始 chunk 同池竞争。
2. **Query 回流**：有价值的问答对沉淀为 synthesis 页（走既有 ingest/lint 管线，含事实保留率探针与矛盾检查）——**系统越用越聪明**，这是"自进化"的核心机制。
3. **Lint 常态化**：定期审计（矛盾/过时/孤儿页），已有 Phase 3 能力，补调度即可。

预期：跨文档桥接由 Agent 行走链接天然解决（正是 A/B 缺口）；成本远低于 GraphRAG 索引。

### 阶段二：MCP 服务化——知识能力对外输出

把 `kb_search` / `wiki_lookup` / `web_search` / 会话记忆包装成 MCP server（复用 ToolManager 与鉴权），任何 MCP 客户端（Claude Code、Cursor、其他 agent）可直接查询本知识库。对齐 DeepWiki 的分发策略；把项目从"一个系统"升级为"组织知识基础设施"。完全离线私有化是相对 DeepWiki/autowiki 的差异化。

### 阶段三：Context Engine 收口——治理与统一入口

- 统一检索 API：多源（KB/wiki/graph/web）+ 自适应路由（已有决策管线）+ 权限过滤（chunk 级权限，roadmap P3 既有项）+ 来源溯源（audit 已有，补 provenance 字段贯通）。
- 知识版本化：wiki 页与文档更新可回滚、可时间点重建。

### 阶段四（远期/创意）：自进化知识运行时

- **Deep Research 模式**（对标 DeepWiki）：长时程多跳研究任务，产出带引用的研究报告并沉淀为 wiki 页。
- **图记忆融合**：wiki 实体图 + agent 会话记忆（L1-a 已灰度）+ 时序事实（Zep/Graphiti 思路），服务长程 agent 工作流。
- **多模态文档智能**（ColPali 思路）按需评估。

---

## 4. 与既有 roadmap 的关系

- 本文档**取代/收编** P2-1（GraphRAG）：A/B 已证 per-doc 编译页不解决跨文档问题，而阶段一的"导航式 wiki + 回流"以更低成本解决同一缺口；GraphRAG 降为阶段四图记忆的可选件。
- P3"学习引擎闭环验收"与阶段一"回流"同源，建议合并实施。
- chunk 级权限（P3）归入阶段三。

## 5. 参考来源

- vanja.io — The Knowledge Base That Builds Itself（Karpathy Pattern 实践）
- idir-mellaz.fr — RAG Isn't Enough: Building the Context Layer（Karpathy Pattern + 企业化缺口）
- codersera / miraheze — DeepWiki Complete Guide 2026（Fast vs Deep Research、MCP server）
- github.com/anyweez/autowiki、npmjs claude-code-wiki、pratiyush/llm-wiki
- acingai.com — RAG in 2026: From Vector Search to Context Engines and GraphRAG
- intuitionlabs.ai — What Is Context Engineering（MCP Linux 基金会标准化、Gartner 40%）
- agentmarketcap.ai — Distyl $1.8B / agent memory graph 化共识；frontiernews.ai — Mem0/Zep/Letta 对比（MAGMA、LongMemEval）
