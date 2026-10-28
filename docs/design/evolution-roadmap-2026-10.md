# 全项目演进与完善路线（2026-10 审计）

> 状态：已确认（2026-10-22 审计定稿；2026-10-24 补安全与多租户专项）
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
| UX-2 | 移动端适配 | ✅ 已实施（2026-10-23）：侧栏抽屉 + 移动顶栏；聊天/历史/知识库/设置/追踪响应式收口；对比面板与图谱详情覆盖层；Markdown 表格内部滚动 | 已完成 |
| UX-3 | 可访问性基础 | ✅ 已实施（2026-10-23）：skip link + 全局焦点环 + reduced-motion；抽屉 inert；消息区 role=log/aria-live；图标按钮 aria-label、评分 aria-pressed、面板 aria-expanded；卡片键盘可操作；Toast/错误态 role | 已完成 |
| UX-4 | 三态组件体系 | ✅ 已实施（2026-10-24）：新增 AppStateView（loading/empty/error + 操作槽 + 错误重试）与 AppSkeleton；统一知识库、历史、设置各页、追踪、图谱、Wiki、搜索与来源弹窗的 14+ 处状态；知识图谱补错误态 | 已完成 |
| UX-5 | 历史页搜索 / 筛选 / 导出 | 仅批量删除 | P2 |
| UX-6 | 长会话性能 | 无虚拟滚动，消息 + Markdown 全量渲染 | P2 |
| UX-7 | 细节增强 | 引用悬浮预览、消息内搜索、导出 Markdown、快捷键、新手引导；对比度全量审计 | P3 |

### 3.3 看着舒服（VIS）

| # | 方向 | 现状 / 证据 | 优先级 |
|---|---|---|---|
| VIS-1 | 设计 token 层 | ✅ 已实施（2026-10-24）：`style.css` 新增语义色（page/surface/content/line/status）、圆角与阴影 token，`.dark` 统一覆盖；全局组件类改用 `var(--token)`；35 个前端文件将 `light + dark:` 成对类迁移为 `bg-surface` / `text-content` / `border-line` 等语义类；SVG/Markdown 硬编码色改走 token | 已完成 |
| VIS-2 | 组件 primitive | ✅ 已实施（2026-10-24）：`components/ui/` 提供 AppButton/AppCard/AppBadge/AppInput/AppSelect/AppDialog（+11 单测）；Button 6 组变体 + 6 种尺寸，Dialog 统一 EP 封装；手写 button 127→38（余量为专用控件），卡片/弹窗/表单完成迁移；删除未使用的全局 btn/card/input/badge 类 | 已完成 |
| VIS-3 | 页面视觉收口 | ✅ 已实施（2026-10-24）：EP 弹窗/下拉/表单/表格 token 化；聊天 768px 主列 + 欢迎态/气泡；知识库移动端工具栏与表格滚动；设置页宽度/侧栏统一；浅深色/移动端截图验证 | 已完成 |

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
| OPS-1 | 运维手册补全 | ✅ 已完成（2026-10-27）：`guide/backup-restore.md` 覆盖备份/恢复/回滚矩阵/容量模型/季度演练 | P1 |
| OPS-2 | 供应链安全 | ✅ 已完成（2026-10-27）：dependabot + Trivy/SBOM CI + Actions v7 + 依赖约束收口；积压 PR 清零 | P1 |
| OPS-3 | 结构化日志 | ✅ 已完成（2026-10-27）：`utils/log_context.py` 统一注入 request_id/user_id/stage，支持 JSON 输出 | P2 |
| OPS-4 | 回归与演练 | 待办：nightly 性能回归未建；恢复/回滚演练步骤与记录模板已就绪（backup-restore.md §7） | P2 |
| OPS-5 | 覆盖率上调 | ✅ 已完成（2026-10-28）：后端 `fail_under=65`、实测 67.37%（1326 passed）；前端阈值 35/40/25/34 | P3 |

### 3.6 安全与多租户（SEC，2026-10-24 对抗性审查）

> 方案与验收见 [security-multitenancy-hardening-2026-10.md](security-multitenancy-hardening-2026-10.md)；任务台账见 [project-status.md](../project-status.md)「安全与多租户加固专项」。

| # | 方向 | 结论 | 优先级 |
|---|---|---|---|
| SEC-1 | 学习模块隔离 | ✅ 已实施（2026-10-24）：execution owner 回填 + 查询过滤 + record-* 收口 | P1 |
| SEC-2 | 注册与资源配额 | ✅ 已实施（2026-10-24）：生产注册护栏 + chat/upload 配额 | P1 |
| SEC-3 | 代理感知限流 | ✅ 已实施（2026-10-24）：受信代理 IP + Redis 滑动窗口 + 降级 | P1 |
| SEC-4 | 状态/索引一致性 | ✅ 已实施（2026-10-24）：下架删向量/上架重建 + KB 移动重建 + 缓存失效 | P1 |
| SEC-5 | Agent 注入与出站 | ✅ 已实施（2026-10-24）：不可信上下文 + 工具域名/DLP + fetch 默认关 + 红队回归 | P1 |
| SEC-6~15 | 企业级加固 | ✅ 已实施（2026-10-27）：实验/通知/分类标签归属、SSRF fail-closed + IP 固定、JWT 吊销、缓存索引、live/ready、备份/供应链、隐私日志、覆盖率 | P2 |
| SEC-16~21 | 纵深与体验 | ✅ 已实施（2026-10-27）：CSP/HSTS、评估配额、会话分页/批量上限、ToolGuard DNS、secrets 预检、文档边界 | P3 |

**上线红线**：SEC-1~5、SEC-6~15、SEC-16~21 均已完成；公网多租户仍按「受控网络 + 最小权限 + 前置反代 TLS/HSTS」运营（§ 见 [security-multitenancy-hardening-2026-10.md](security-multitenancy-hardening-2026-10.md)）。

## 4. 执行批次

**批次 0：安全加固（公网前置，P1）——✅ 已完成（2026-10-24）**
- SEC-1 学习隔离 → SEC-3 代理限流 → SEC-2 注册/配额 → SEC-4 状态/索引 → SEC-5 Agent 出站

**批次 0.5：安全 P2（企业级）——✅ 已完成（2026-10-27）**
- SEC-6 实验 → SEC-8 通知 → SEC-7 分类标签 → SEC-9 SSRF → SEC-10 JWT → SEC-11 缓存索引 → SEC-12 健康 → SEC-13 运维/供应链 → SEC-14 隐私 → SEC-15 测试

**批次 0.6：安全 P3（纵深）——✅ 已完成（2026-10-27）**
- SEC-19 ToolGuard DNS → SEC-16 CSP/TLS → SEC-17 评估配额 → SEC-18 分页/上限（SEC-20/21 已完成）

**批次 1：体感与观感（P1，优先）**
- UX-1 停止生成 / 编辑重发；UX-2 移动端；UX-3 a11y 基础；UX-4 三态组件
- VIS-1 设计 token；VIS-2 组件 primitive
- AGT-2 在线抽样脚本（trace → badcase）

**批次 2：可靠性与代码观感**
- AGT-1 / AM-4 轻量 checkpoint + 断线恢复（待办）
- CODE-1 拆分 `document.py` / `web_search_service.py` / `chat.py`（待办）
- CODE-2 异常分级（待办）；OPS-1 运维手册 / OPS-2 供应链扫描 / OPS-3 结构化日志（✅ 2026-10-27）

**批次 3：差异化能力**
- AGT-4 Artifacts 工作区；AGT-5 MCP 客户端；AGT-6 记忆 / 上下文工程（待办）
- OPS-4 nightly 与恢复演练（待办）；OPS-5 覆盖率上调（✅ 2026-10-28）

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
