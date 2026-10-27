# 文档索引

> 命名与组织方式参考 Django / Vue / FastAPI 等项目惯例：核心参考置于顶层，其余按性质分目录；文件名使用小写 kebab-case。

## 目录与命名规则

| 目录 | 职责 | 归档/流转规则 |
|---|---|---|
| 顶层（`architecture.md`、`api.md`、`project-status.md`） | 随代码持续维护的核心参考；`project-status.md` 另承担「已完成 / 剩余待办」台账职责 | 代码结构或接口变更时必须同步更新 |
| `guide/` | 操作指南（部署、开发、监控） | 与实际操作流程保持一致，流程变更即更新 |
| `design/` | 设计文档（对标 RFC），一文一事 | 遵循「设计先行 → 确认后实施」；实施完成后回写实施记录章节并更新状态；完全落地且不再单独演进后可移入 `archive/` |
| `archive/` | 归档（一次性报告、已完结文档） | 只读封存，不再更新；文件名可带日期后缀 |

**命名**：小写 kebab-case，见名知义（如 `env-isolation-and-workflow-refactor.md`）。

**设计文档状态规范**（头部必须有一行 `> 状态：`，与 `docs/README.md` 状态表双向同步）：

| 状态 | 含义 |
|---|---|
| 草稿 | 设计编写中，未定稿 |
| 待确认 | 设计定稿，等待确认后实施 |
| 部分实施 | 部分条目落地；文档内须逐项标注进度（如 ✅） |
| 已实施 | 全部落地；注明完成日期，实施记录回写文档末尾 |
| 已归档 | 移入 `archive/`，不再维护 |

## 目录结构

```
docs/
├── README.md            # 本索引
├── architecture.md      # 架构总览：服务拓扑、RAG 决策管线、代码地图
├── api.md               # API 参考：全部接口说明
├── project-status.md    # 项目状态与待办台账（跨专项：已完成 / 剩余待办 / 阻塞项 / 红线）
├── guide/               # 操作指南
│   ├── deployment.md    #   部署（开发/生产环境）
│   ├── development.md   #   开发与测试（含踩坑记录）
│   └── monitoring.md    #   监控运维（指标、告警、处置）
├── design/              # 设计文档（对标 RFC），状态见下表
│   ├── improvement-roadmap.md
│   ├── rag-enhancement.md
│   ├── intent-router-upgrade.md
│   ├── search-optimization.md
│   ├── price-trustworthiness.md
│   ├── llm-wiki-compile.md
│   ├── env-isolation-and-workflow-refactor.md
│   ├── chat-latency-and-model-config.md
│   ├── frontend-test-plan.md
│   ├── semantic-cache.md
│   ├── severe-fixes.md
│   ├── agent-evolution.md
│   ├── agent-ab-evaluation.md
│   ├── agent-maturity-gap-analysis.md
│   ├── agent-eval-trace.md
│   ├── agent-guardrails-hitl.md
│   ├── security-multitenancy-hardening-2026-10.md
│   ├── ui-visual-polish-2026-10.md
│   ├── model-provider-registry-2026-10.md
│   ├── evolution-roadmap-2026-10.md
│   ├── evolution-research-2026-09.md
│   ├── wiki-navigable-workspace.md
│   ├── e1-navigable-lookup-gap.md
│   ├── learning-loop-wiring.md
│   ├── prod-schema-drift-repair.md
│   ├── wiki-link-metadata-edges.md
│   └── special-projects-2026-09.md
└── archive/             # 归档（一次性报告、已收官计划）
    ├── code-review-2026-08.md      # 代码审查报告（已全部修复）
    ├── code-review-2026-09.md      # 代码审查报告（4 项阻断问题已修复；文档漂移已全量回写）
    ├── code-review-2026-09-23.md   # 代码审查报告（P0×1 + P1×5 + P2×17；P1 已修复入库）
    ├── code-review-2026-09-24.md   # 代码审查报告（已全部修复：W1~W6 全部落地）
    ├── fix-plan-2026-09-24.md      # 四份评审综合修复计划（2026-09-29 收官，78 项 P2 全部落账）
    ├── project-readthrough-2026-09-30.md  # 项目通读报告（2026-09-30 只读代码审查）
    └── direct-model-switch-plan-2026-10-03.md  # 直答模型切换与 A/B 验证（已完结计划，2026-10-15 归档）
```

## design/ 文档状态表

| 文档 | 状态 | 说明 |
|---|---|---|
| [improvement-roadmap.md](design/improvement-roadmap.md) | 部分实施 | 执行计划；P0-1/P1-1/P1-2/P1-3 及 P2-3~P2-6 已完成（多查询检索经 A/B 评估保持默认关闭），进度以各条目 ✅ 标记为准 |
| [rag-enhancement.md](design/rag-enhancement.md) | 部分实施 | 混合检索/重排序/评估闭环/意图路由已落地（16 项中 15 项完成，剩前端分块策略选择）；分阶段进度并入 roadmap 推进 |
| [intent-router-upgrade.md](design/intent-router-upgrade.md) | 已实施 | `backend/src/services/intent_router/`（LLMRouter / ConfidenceGate / 历史上下文增强） |
| [search-optimization.md](design/search-optimization.md) | 已实施 | 通用联网搜索链路优化（Query 改写 / 引用补全 / AnswerVerifier）；逐项核对见 §5.1 |
| [price-trustworthiness.md](design/price-trustworthiness.md) | 已实施 | 垂类结构化工具（金价/汇率/天气/时间）已落地于 `backend/src/services/tools/plugins/` |
| [llm-wiki-compile.md](design/llm-wiki-compile.md) | 已实施 | Phase 1~5 全部完成（2026-09-07 ~ 09-09），实施记录见 §8/§10/§11/§13/§14 |
| [env-isolation-and-workflow-refactor.md](design/env-isolation-and-workflow-refactor.md) | 已实施 | P0+P1 完成（2026-09-08），dev/prod 双栈并存验证通过，CI 已接入 env-consistency |
| [chat-latency-and-model-config.md](design/chat-latency-and-model-config.md) | 已实施 | 首字延迟治理 + 模型配置去硬编码（2026-09-07，记录见 §10）；2026-10-03 二次优化 + generate 瓶颈诊断与小模型选型建议见 §11；直答模型 A/B 定稿见 §12，reranker 换型等后续实测见 §13 |
| [frontend-test-plan.md](design/frontend-test-plan.md) | 已实施 | 前端单测 + E2E 体系（P2-6，2026-09-06） |
| [semantic-cache.md](design/semantic-cache.md) | 已实施 | 语义缓存 P1-3（2026-09-07），记录见 §8 |
| [severe-fixes.md](design/severe-fixes.md) | 已实施 | 严重问题 1~9 修复；来源报告归档于 archive/code-review-2026-08.md |
| [agent-evolution.md](design/agent-evolution.md) | 部分实施 | Agent 有界循环 Phase 1/2 已落地，L1-a 记忆已实施（见 archive/code-review-2026-09.md §6.5） |
| [agent-ab-evaluation.md](design/agent-ab-evaluation.md) | 已执行验收 | A/B 两臂 live 对比完成（2026-09-14），结果与未达标项见 §7.1 |
| [agent-maturity-gap-analysis.md](design/agent-maturity-gap-analysis.md) | 已确认 | Agent 成熟度对标与差距分析（2026-10-20）：对标业界做法与主流产品，给出能力矩阵/差距清单/优先级；落地项见 [project-status.md](project-status.md) §二「Agent 成熟度差距」 |
| [agent-eval-trace.md](design/agent-eval-trace.md) | 部分实施 | Agent 轨迹级评测（AM-1）：①②③ 已实施（2026-10-20，单测 +16；`AGENT_TRAJ_EVAL_ENABLED` 默认关）；④ 在线抽样待办 |
| [agent-guardrails-hitl.md](design/agent-guardrails-hitl.md) | 已实施 | Agent 护栏与 HITL（AM-2，2026-10-22）：输入/工具/输出三层确定性护栏 + 审批闸门（`AGENT_APPROVAL_MODE` 默认 off，审批名单为空时不触发）；端到端 SSE `approval_required` + 回执 API + 前端审批卡片 |
| [security-multitenancy-hardening-2026-10.md](design/security-multitenancy-hardening-2026-10.md) | 部分实施 | 安全与多租户加固（2026-10-24 对抗性审查）：P1×5 已完成；P2 中 SEC-6/7/8/10/11/12 完成、SEC-9/13/14/15 部分完成；P3×6 中 SEC-20/21 已完成、SEC-18 部分完成。任务拆分见 [project-status.md](project-status.md)「安全与多租户加固专项」 |
| [ui-visual-polish-2026-10.md](design/ui-visual-polish-2026-10.md) | 已实施 | 前端视觉收口（VIS-3，2026-10-24）：8px 网格/768px 聊天主列、EP 弹窗/下拉/表单/表格 token 化、侧栏与知识库/设置页密度收口；浅深色与移动端截图验证 |
| [model-provider-registry-2026-10.md](design/model-provider-registry-2026-10.md) | 部分实施 | 模型 Provider/Model/Route 三层：P1/P2/P2.1/P3 已完成（2026-10-26）；含熔断、用量/成本持久化、设置页展示、Embedding 影子集合重建/回滚与可选 rerank 路由；剩余 P4（Anthropic/Gemini/Azure、租户/成本策略） |
| [evolution-roadmap-2026-10.md](design/evolution-roadmap-2026-10.md) | 已确认 | 全项目演进与完善路线（2026-10-22 审计）：业界「标配/差异化/避免」对标 + 代码/UX/视觉/运维差距证据 + 三批次执行序；任务条目见 [project-status.md](project-status.md) §二「全项目完善与演进专项」 |
| [evolution-research-2026-09.md](design/evolution-research-2026-09.md) | 已确认 | 演进方向调研（2026-09-23）：从 RAG 问答到自进化知识运行时，四阶段路线于 2026-09-29 owner 拍板转正，立项见 special-projects-2026-09.md |
| [wiki-navigable-workspace.md](design/wiki-navigable-workspace.md) | 已实施 | 阶段一：Wiki 升级为 Agent 可导航工作空间（D1/D3/D4 落地；D2/D5 经 A/B 证伪后回退默认值，见 §10） |
| [special-projects-2026-09.md](design/special-projects-2026-09.md) | 部分实施 | 技术债与演进专项立项 A~E；C / D / E.6 已完成、E-1 差距盘点完成待实施（进展与证据见文末「实施进展回写」）；A/B 按触发器挂起 |
| [e1-navigable-lookup-gap.md](design/e1-navigable-lookup-gap.md) | 部分实施 | E-1 导航式 wiki_lookup：W1/W2/W3 已实施（2026-10-15）；E1V 离线**两轮**（10-19/10-20）——**主瓶颈为编译覆盖**（`_parse_json_dict` 已修），修后 C 臂 0.188→0.938、图边 47 含跨档 13；**仅 Agent e2e 待办** |
| [wiki-link-metadata-edges.md](design/wiki-link-metadata-edges.md) | 部分实施 | Wiki 图边「仅元数据建边」（L1 已实施 + 编译覆盖修复 + 离线验证，2026-10-20）；L2 暂缓，导航须 Agent e2e |
| [learning-loop-wiring.md](design/learning-loop-wiring.md) | 已实施 | 学习闭环接入（LEARN）：W-A 权重回载 / W-B 样本补采 / W-C A/B 接入 chat（2026-10-16 单测 + dev 端到端验证通过），三开关灰度默认关 |
| [prod-schema-drift-repair.md](design/prod-schema-drift-repair.md) | 已实施 | 生产库 schema 漂移修复：代码侧（幂等对账迁移 + 去 start-prod stamp + CI `alembic check` 卡点）+ prod 窗口执行（`upgrade head` 后 `alembic check` 无漂移）全部完成（2026-10-19） |
| [fix-plan-2026-09-24.md](archive/fix-plan-2026-09-24.md) | 已归档 | 四份评审综合修复计划（2026-09-29 收官：W0-2 完成、W1~W5 全部完成、W6 八个批次完成，78 项 P2 全部落账）；剩余仅用户侧操作 W0-1/W0-3，见文档头部状态 |
| [project-readthrough-2026-09-30.md](archive/project-readthrough-2026-09-30.md) | 已归档 | 项目通读报告（2026-09-30 只读代码审查） |
| [direct-model-switch-plan-2026-10-03.md](archive/direct-model-switch-plan-2026-10-03.md) | 已归档 | 直答模型切换与 A/B 验证执行方案（2026-10-03 定稿；2026-10-15 移入 archive/ 封存） |

## 按场景查找

| 场景 | 阅读顺序 |
|---|---|
| 新人上手 | 根 [README](../README.md) → [architecture.md](architecture.md) → [guide/deployment.md](guide/deployment.md) |
| 本地开发/跑测试 | [guide/development.md](guide/development.md) |
| 排查线上问题 | [guide/monitoring.md](guide/monitoring.md) → [guide/development.md](guide/development.md) 踩坑记录 |
| 查接口 | [api.md](api.md)（另有 Swagger UI: `http://localhost:8000/docs`） |
| 配置/切换模型 | 设置 → 模型服务（`/settings/models`）→ [design/model-provider-registry-2026-10.md](design/model-provider-registry-2026-10.md) → [guide/deployment.md](guide/deployment.md) §6.3.2 |
| 了解技术决策/规划 | [project-status.md](project-status.md) → [design/special-projects-2026-09.md](design/special-projects-2026-09.md) → [design/improvement-roadmap.md](design/improvement-roadmap.md) |
| 跟进实施状态 | [project-status.md](project-status.md)（已完成/待办台账）→ 本文件「design/ 文档状态表」→ 对应文档的实施记录章节 |
| 公网上线/安全评估 | [security-multitenancy-hardening-2026-10.md](design/security-multitenancy-hardening-2026-10.md) → [project-status.md](project-status.md)「安全与多租户加固专项」→ [agent-guardrails-hitl.md](design/agent-guardrails-hitl.md) |
