# Agent 成熟度对标与差距分析

> 状态：已确认（2026-10-20 调研定稿）
> 关联：[agent-evolution.md](agent-evolution.md)（有界 Agent 设计；§6.1 曾暂缓 checkpoint/HITL）、[agent-ab-evaluation.md](agent-ab-evaluation.md)、[special-projects-2026-09.md](special-projects-2026-09.md)、[project-status.md](../project-status.md)（§二「Agent 成熟度差距」）
> 说明：对标业界成熟 Agent 做法与主流产品，给出本项目的能力矩阵、差距清单与优先级。落地项已纳入台账。

## 1. 结论摘要

业界 2026 共识：**先交付最简方案（workflow），复杂度按实测收益递增**；**工具层已收敛到 MCP**；工程重心从「编排花样」转向**上下文工程、评测可观测、可靠性与护栏**。

对照结论——本项目**强在离线自托管、检索接地与延迟/成本治理**，**弱在工具互操作协议、持久化执行、人在环、轨迹级评测**（末项正是 E1V 卡住的根因）。

**Top 3 行动**：① Agent 轨迹级评测 → ② MCP 客户端接入 → ③ HITL 审批点 + 输入/输出护栏。

## 2. 成熟做法综述（要点）

- **Workflow vs Agent**：workflow = 预定义代码路径编排 LLM/工具；agent = LLM 自主决定流程。**仅当更简单方案不足时才加复杂度**（延迟/成本随智能体程度上升）。[cite:1]
- **五种 workflow**：prompt chaining / routing / parallelization / **orchestrator-workers** / **evaluator-optimizer**。[cite:1]
- **多 Agent**：生产系统用 orchestrator-worker（lead + 并行 subagent），内部研究评测**超单 agent 90.2%**；但**强依赖型任务（如编码）不宜**。成本：agent≈4× chat token、multi-agent≈15×；**token 用量解释 80% 性能方差**。[cite:3]
- **状态与恢复**：LangGraph 标配 **checkpointer**（线程内短期记忆、HITL、time travel、容错）+ **store**（跨线程长期记忆）；HITL 基于 **interrupt + checkpoint**（暂停持久、可恢复）。[cite:8][cite:9]
- **上下文工程**：上下文是有限资源，存在 **context rot** 与注意力预算 → 最小高信号 token、just-in-time 检索；长任务用 **compaction**、结构化笔记、多 Agent。[cite:2]
- **工具设计**：自包含、无重叠、命名空间清晰；「**若工程师都说不清该用哪个工具，agent 更不行**」；按 token 效率优化返回。[cite:4]
- **护栏**：输入/输出/工具三类 guardrail，**tripwire 触发即中止**；可用便宜快模型前置校验。[cite:7]
- **评测**：离线（回归/发布门禁）+ **在线（生产 trace，评轨迹/效率/合规）**；小样本（~20 例）起步；LLM-as-judge 打分 0–1 + pass/fail 最稳。[cite:15]
- **协议层**：**MCP** 已成事实标准（Claude/ChatGPT/VS Code/Cursor 等；Google Cloud 50+ 托管 MCP 服务器）；**A2A** 已捐 Linux Foundation，150+ 组织支持。[cite:11][cite:12][cite:13]

## 3. 产品对标矩阵（as of 2026-10）

| 产品 | 自主性 | 工具/MCP | 记忆 | 多Agent | HITL | 可观测 | 安全 | 部署 |
|---|---|---|---|---|---|---|---|---|
| OpenAI | 混合 | **MCP✓** | 会话记忆 | — | 可接管 | — | auto-review | 云 |
| Claude | 混合 | **MCP+hooks+技能** | **压缩+memory tool** | **子Agent×5层** | **权限审批** | Compliance API | **多层注入防御** | 云+自托管沙箱 |
| Google | 混合(ADK) | **MCP✓** | — | 编排 | Inbox 监控 | Agent DevOps | 企业治理 | 云 |
| Perplexity | 自主 | — | 跨标签 | — | 可中断 | 步骤日志 | — | 云 |
| 编码类(Cursor/Devin/Copilot) | **自主** | MCP✓ | Devin 记忆+Dreaming | 并行 Agent | PR 审批 | 评审/Bugbot | — | IDE 本地+云 |
| Manus | **自主** | MCP 连接器 | — | 多子 Agent | 浏览器接管 | — | — | 云电脑 |
| 中国生态(Coze/百炼/Dify/智谱) | 混合 | MCP 市场+插件 | 云盘 | 多 Agent | — | — | — | **Dify 可自托管** |

> 数据时点 2026-10；中文生态部分条目为 P2 来源，存疑项已在调研原始清单标注。**工具层收敛 MCP；自主性普遍「编排+自主」混合；闭源几乎全是云托管，自托管稀缺**。

## 4. 本项目 vs 业界：能力矩阵

| 维度 | 业界成熟态 | 本项目 | 判定 |
|---|---|---|---|
| 编排 | ReAct / plan-execute / 多 Agent | **有界 plan-execute + 单 Agent**（[agent_orchestrator.py#L44-L69](../../backend/src/services/agent_orchestrator.py#L44-L69)、步数/时间预算 #L431-L434） | ✅ 对齐官方「够用即可」 |
| 工具层 | 自包含 + 命名空间 + **MCP** | 插件工具 + kb/web/wiki；**无 MCP 实现**（仅 [「阶段二 MCP 前置」](../../backend/src/models/user_api_key.py#L1-L4)） | ❌ 缺协议层 |
| 记忆 | 短期 + 长期 store + compaction | L1-a 记忆（`AGENT_MEMORY_ENABLED` 默认关）；**无跨会话长期存储/压缩** | ⚠️ 弱 |
| 检索接地 | RAG + 引用可追溯 | **混合 dense+BM25+RRF+rerank**、多查询、[context_builder](../../backend/src/services/context_builder.py#L87-L125) token 预算 + Lost-in-Middle + 编号引用 | ✅ **优势** |
| 多 Agent | orchestrator-worker | 无（单 Agent + 工具） | ⚪ 视场景 |
| HITL | interrupt + checkpoint / 审批 | **未见实现**（`interrupt` 命中均为 SSE 断连计数） | ❌ 缺 |
| 持久化/恢复 | checkpointer、durable execution | **未见实现**（循环为内存态） | ❌ 缺 |
| 可观测/评测 | trace + 离线回归 + **在线/轨迹级评测** | RequestTrace + `stages` 埋点 + 离线 A/B harness；**无 agent 轨迹级/在线评测** | ⚠️ 半 |
| 护栏 | 输入/输出/工具三类 + tripwire | KB 白名单/owner 隔离注入（[#L340-L395](../../backend/src/services/agent_orchestrator.py#L340-L395)）、输出截断去重、认证限流；**无注入防护/tripwire** | ⚠️ 半 |
| 部署 | 云托管为主 | **完全离线自托管**（Ollama + Milvus/MinIO/PG/Redis，多栈并存） | ✅ **差异化** |
| 成本/延迟 | 分层模型 + 缓存 | fast/主模型分工（4B 仅 `deep_thinking`）、精确+语义缓存、跳过 LLM 裁决、辅助任务后台化 | ✅ **优势** |

## 5. 差距清单与优先级

| 优先级 | 差距 | 为什么现在 | 建议 |
|---|---|---|---|
| **P1** | **无 Agent 轨迹级评测** | 一切 Agent 改进缺度量——E1V「离线 5-slot 测不到导航」即此缺口的直接后果 | 20 例起步的 agent e2e 集 + 轨迹级 LLM-judge（离线回归 + 在线 trace） |
| **P1** | **无 HITL 审批/中断** | 工具一旦有写操作（发消息/改数据）无人工确认点 | `interrupt` 语义 + 审批点（先只挂高危工具） |
| **P2** | **无 MCP 客户端** | 业界已收敛 MCP，本库工具只能自研 | 先做 MCP **客户端**（消费外部工具）；服务端暴露随后（API Key 前置已就绪） |
| **P2** | **无持久化/可恢复执行** | 长任务/多步 Agent 中断即丢 | 会话级 checkpoint + 幂等工具执行 |
| **P2** | **记忆弱**（默认关、无长期存储） | 多轮个性化与跨会话沉淀受限 | 长期 store + compaction（对齐 context rot 治理） |
| **P2** | **护栏半成品** | 提示注入是当前安全焦点 | 输入/输出 tripwire + 工具级权限；便宜模型前置校验 |
| **P3** | 多 Agent / 工具数量治理 / **同名跨档页合并** | 视场景收益；末项为 wiki 编译质量缺陷 | 先测收益再上；同名页合并单独议 |

> **与本项目既有决策的对齐**：[agent-evolution.md](agent-evolution.md) §6.1（2026-09-14）曾判「**暂无 checkpoint 断点恢复与人机协同中断需求，维持现状即最优**」。本分析**不推翻**该结论——而是在 P1 重新登记为**候选待办**：触发条件（多轮工具依赖成为刚需 / 高危写操作工具上线）出现时启动。E-1 的 E1V 卡点则直接印证了**评测缺口**的代价。

## 6. SWOT 速览

| | 正面 | 负面 |
|---|---|---|
| **内部** | **S**：离线自托管（数据不出域）+ 检索可追溯 + 延迟/成本治理（分层模型、缓存） | **W**：协议层 / 状态持久化 / HITL / 轨迹评测 四项缺失 |
| **外部** | **O**：企业对「本地、可审计、可追溯」Agent 的需求；闭源几乎全云托管，**自托管是稀缺位** | **T**：云厂商 MCP 生态与多 Agent 能力快速拉开；注入攻击面扩大 |

## 7. 建议动作

1. 先做 **Agent 轨迹级评测**（复用现有 trace + `tests/evaluation/`），同时解锁 E1V 终判。
2. 补 **HITL + 护栏**（安全底线，成本低）。
3. 按原计划推进 **MCP 客户端**（阶段二前置已铺）；**记忆/持久化**随后。
4. 多 Agent **缓**——官方立场「先最简」，本已对齐。

## Sources

- [cite:1] [Building effective agents — Anthropic](https://www.anthropic.com/engineering/building-effective-agents)
- [cite:2] [Effective context engineering for AI agents — Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [cite:3] [How we built our multi-agent research system — Anthropic](https://www.anthropic.com/engineering/multi-agent-research-system)
- [cite:4] [Writing effective tools for AI agents — Anthropic](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [cite:5] [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
- [cite:7] [Guardrails — OpenAI Agents SDK](https://openai.github.io/openai-agents-python/guardrails/)
- [cite:8] [Persistence — LangGraph Docs](https://docs.langchain.com/oss/python/langgraph/persistence)
- [cite:9] [Interrupts / Human-in-the-Loop — LangGraph Docs](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [cite:10] [Multi-agent systems — Google ADK](https://adk.dev/agents/multi-agents/)
- [cite:11] [What is MCP? — Model Context Protocol](https://modelcontextprotocol.io/)
- [cite:12] [Google-managed MCP servers — Google Cloud Blog](https://cloud.google.com/blog/products/ai-machine-learning/google-managed-mcp-servers-are-available-for-everyone)
- [cite:13] [A2A Protocol Surpasses 150 Organizations — Linux Foundation](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- [cite:14] [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/pdf/2303.11366v4.pdf)
- [cite:15] [Evaluating AI Agents at the Run, Trace, and Thread Level — LangChain](https://www.langchain.com/resources/agent-evals)
