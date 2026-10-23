# 全项目演进与完善路线（2026-10 审计）

> 状态：已确认（2026-10-22 审计定稿）
> 关联：[project-status.md](../project-status.md)（唯一任务台账）、[agent-maturity-gap-analysis.md](agent-maturity-gap-analysis.md)、[agent-guardrails-hitl.md](agent-guardrails-hitl.md)
> 目标：不仅「好用」（能力与交互），也要「好看」（设计系统与代码观感）。

## 1. 对标结论（2026）

调研 OpenAI Agents SDK、Claude Agent SDK / Claude Code、Google ADK、LangGraph / LangSmith、
MCP 2026-07 规范，以及 ChatGPT / Claude / Manus / Devin / Cursor / Perplexity / Notion AI 的产品形态。

| 类别 | 结论 |
|---|---|
| **标配（不做就出局）** | 流式回答 + 行内引用/来源卡；单 Agent + ≤20 个精选工具；会话持久化 / 断点续跑 / **可取消**；不可逆动作前 HITL；输入 / 输出 / 动作三层护栏；离线评测集 + 确定性断言；trace / 审计日志 |
| **差异化（值得投入）** | trace → 缺陷 → 回归样例 → 门禁的闭环；上下文工程（大结果落盘、摘要压缩、按需 Skills）；主力 + 廉价 sidekick 双模型路由；产物工作区（Artifacts）+ 版本 / diff + 一键接管；模型无关、记忆存自有 PG；按动作风险分级自治 |
| **应避免（过度设计）** | 为问答强上多 Agent（同任务 3–10× token）；几十个工具全量暴露；规划 / 执行 / 评审三角色全家桶；无人值守长跑；自建评测平台 / 自训 judge；默认开放 computer-use |

**关键判断**：本项目的差异化在「离线自托管 + 检索接地 + 延迟/成本治理」，
应把投入放在**可恢复、可评测、可接管、可看**，而不是堆 Agent 自主性。

## 2. 本次审计证据（2026-10-22）

| 维度 | 现状 |
|---|---|
| 后端单测 | 1103 passed / 105 skipped，覆盖率 **62.12%**（门槛 60） |
| 前端单测 | 109 passed（16 个文件）；E2E 5 passed |
| 前端静态检查 | eslint **0 error / 43 warning**（全为非空断言，集中在测试） |
| 后端异常 | **334 处** `except Exception`；`TODO/FIXME` 为 0 |
| 前端可访问性 | 全站仅 **5 处** `aria-/role=` |
| 前端响应式 | 仅 **8 处** `sm:/md:/lg:/xl:` 断点类；移动端基本未适配 |
| 加载态 | 仅 **2 处** `animate-pulse`；无 skeleton 体系 |
| 颜色治理 | **22 处**硬编码 hex；`dark:` 171 处但无统一 token 层 |
| 历史页 | 仅见批量删除，无搜索 / 筛选 / 导出入口 |
| 停止生成 | 无用户可点的停止按钮；仅组件卸载时 abort |
| 供应链 | CI 仅 gitleaks；无 dependabot / trivy / SBOM |
| 运维文档 | `docs/guide` 仅 deployment / development / monitoring；无备份 / 恢复 / 回滚 / 容量 |

超大文件（按行数）：

| 后端 | 行数 | 前端 | 行数 |
|---|---|---|---|
| `api/document.py` | 1679 | `queries/kb.ts` | 967 |
| `services/web_search_service.py` | 1040 | `views/Settings/ExperimentView.vue` | 582 |
| `api/chat.py` | 1026 | `components/KnowledgeGraph.vue` | 511 |
| `services/wiki_compiler.py` | 1000 | `views/KnowledgeBaseView.vue` | 465 |
| `services/milvus_service.py` | 918 | `composables/useChatStream.ts` | 424 |
| `services/document_processor.py` | 894 | `components/knowledge-base/WikiDrawer.vue` | 388 |
| `services/search_agent.py` | 875 | `components/knowledge-base/UploadDocumentDialog.vue` | 386 |
| `services/pipeline/stages.py` | 783 | `components/chat/ChatMessageList.vue` | 327 |
| `services/agent_orchestrator.py` | 740 | `components/chat/ChatInputArea.vue` | 304 |

## 3. 差距与方向（任务台账同步）

### 3.1 Agent 能力（AGT）

| # | 方向 | 现状 / 证据 | 优先级 |
|---|---|---|---|
| AGT-1 | 会话可恢复 + 可取消（AM-4） | 循环纯内存态；无停止按钮；断线即丢 | P1 |
| AGT-2 | trace → 回归样例闭环（AM-1④） | 只有离线 A/B，无线上抽样与失败转回归 | P1 |
| AGT-3 | 评测可信度补齐 | `hybrid_01` 双源硬保证；`hybrid_04` 拒答复核；非 Agent 路径跳过判分 | P1 |
| AGT-4 | 产物工作区（Artifacts） | 仅 Markdown 气泡 + 来源卡，无预览 / 修订 / 下载 / 版本 | P2 |
| AGT-5 | MCP 客户端（AM-3） | 工具只能自研，无外部工具互操作 | P2 |
| AGT-6 | 记忆 / 上下文工程（AM-5） | 仅会话摘要且默认关；无压缩、大结果落盘、长期 store | P2 |
| AGT-7 | 多模态输入 / 沙箱执行 | 输入区无图片/文件入口；无代码执行 | P3 |

### 3.2 好用（UX）

| # | 方向 | 现状 / 证据 | 优先级 |
|---|---|---|---|
| UX-1 | 停止生成 / 编辑重发 | ✅ 停止生成已落地（2026-10-23）：生成中停止按钮 + 部分回答落库；编辑重发待做 | 进行中 |
| UX-2 | 移动端适配 | 全前端仅 8 处响应式断点 | P1 |
| UX-3 | 可访问性基础 | 仅 5 处 aria/role；键盘 / focus / 对比度未治理 | P1 |
| UX-4 | 三态组件体系 | 仅 2 处 skeleton；加载/空/错各页不统一 | P2 |
| UX-5 | 历史页搜索 / 筛选 / 导出 | 仅批量删除 | P2 |
| UX-6 | 长会话性能 | 无虚拟滚动，消息 + Markdown 全量渲染 | P2 |
| UX-7 | 细节增强 | 引用悬浮预览、消息内搜索、导出 Markdown、快捷键、新手引导 | P3 |

### 3.3 看着舒服（VIS）

| # | 方向 | 现状 / 证据 | 优先级 |
|---|---|---|---|
| VIS-1 | 设计 token 层 | 无 tailwind config，22 处硬编码 hex；dark 无统一变量 | P1 |
| VIS-2 | 组件 primitive | Button/Card/Dialog/Badge/Input 各处手写，风格漂移 | P1 |
| VIS-3 | 页面视觉收口 | 设置页、知识库页、聊天页密度/圆角/阴影不一致 | P2 |

### 3.4 代码观感（CODE）

| # | 方向 | 现状 / 证据 | 优先级 |
|---|---|---|---|
| CODE-1 | 巨型文件拆分 | 见 §2 表；SA「触碰即拆」扩围到这批文件 | P1 |
| CODE-2 | 异常分级治理 | 334 处宽泛异常；缺「可恢复/需上报/编程错误」分级 | P1 |
| CODE-3 | eslint warning 清零 | ✅ 已修复（2026-10-23）：43 处非空断言清零，规则升 `error`；eslint 0 warning / vue-tsc 0 / 109 单测通过 | 已完成 |
| CODE-4 | API/类型分层 | `queries/kb.ts` 967 行混合请求/类型/转换 | P2 |

### 3.5 工程与运维（OPS）

| # | 方向 | 现状 / 证据 | 优先级 |
|---|---|---|---|
| OPS-1 | 运维手册补全 | 无备份 / 恢复 / 回滚 / 容量规划文档 | P1 |
| OPS-2 | 供应链安全 | 无 dependabot / trivy / SBOM | P1 |
| OPS-3 | 结构化日志 | stdlib 文本日志，未统一注入请求上下文 | P2 |
| OPS-4 | 回归与演练 | 无 nightly 性能回归、无迁移回滚演练 | P2 |
| OPS-5 | 覆盖率上调 | 62.12% → 65%（只升不降） | P3 |

## 4. 执行批次

**批次 1：体感与观感（P1，优先）**
- UX-1 停止生成 / 编辑重发；UX-2 移动端；UX-3 a11y 基础；UX-4 三态组件
- VIS-1 设计 token；VIS-2 组件 primitive
- AGT-2 在线抽样脚本（trace → badcase）

**批次 2：可靠性与代码观感**
- AGT-1 / AM-4 轻量 checkpoint + 断线恢复
- CODE-1 拆分 `document.py` / `web_search_service.py` / `chat.py`
- CODE-2 异常分级；OPS-1 运维手册；OPS-2 供应链扫描；OPS-3 结构化日志

**批次 3：差异化能力**
- AGT-4 Artifacts 工作区；AGT-5 MCP 客户端；AGT-6 记忆 / 上下文工程
- OPS-4 nightly 与恢复演练；OPS-5 覆盖率上调

## 5. 非目标（明确不做）

- 为问答引入多 Agent 编排（除非评测证明单 Agent 不足）
- 工具全量暴露、无风控的长期记忆
- 自建评测平台 / 自训 judge
- 无人值守长任务 / 默认开放 computer-use

## 6. 验收口径

- 每项落地必须附：单测（或 E2E）、trace/metric 证据、文档回写；
- P1 项需在 `project-status.md` 更新状态与证据链接；
- 视觉项以「设计 token 覆盖率 + 组件复用率 + 移动端无横向滚动」为验收；
- 性能项以 P95 不劣化为底线（沿用 A/B 非退化约定）。

## Sources

- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
- [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
- [Building multi-agent systems: when and how to use them](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them)
- [Google ADK: Why evaluate agents](https://adk.dev/evaluate/)
- [LangGraph Checkpointers](https://docs.langchain.com/oss/javascript/langgraph/checkpointers)
- [LangSmith Engine](https://www.langchain.com/langsmith/engine)
- [Context Engineering: How Deep Agents Remember](https://www.langchain.com/blog/context-engineering-deep-agents-remember)
- [OWASP LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- [MCP 2026-07-28 Specification](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [Cursor Changelog: Subagents, Skills, and Image Generation](https://cursor.com/changelog/2-4)
- [Devin Advanced Capabilities](https://docs.devin.ai/work-with-devin/advanced-capabilities)
- [Claude Artifacts Tutorial](https://claude.com/resources/tutorials/use-artifacts-to-visualize-and-create-ai-apps-without-ever-writing-a-line-of-code)
- [Manus Output, Artifacts & Refinement UX](https://aiuxplayground.com/teardowns/manus/output/)
- [Notion Releases](https://www.notion.com/releases)
