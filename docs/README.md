# 文档索引

> 命名与组织方式参考 Django / Vue / FastAPI 等项目惯例：核心参考置于顶层，其余按性质分目录；文件名使用小写 kebab-case。

## 目录与命名规则

| 目录 | 职责 | 归档/流转规则 |
|---|---|---|
| 顶层（`architecture.md`、`api.md`、`FINAL-REPORT.md`） | 核心参考；`FINAL-REPORT.md` 是结项说明（是什么 / 做到了什么 / 没做什么），2026-11-07 冻结 | 代码结构或接口变更时必须同步更新；结项说明不再改 |
| 顶层 `project-status.md` | **已归档**的跳转页，全文见 `archive/project-status-2026-11-07.md` | 不再更新（保留原路径以免旧链接 404） |
| `guide/` | 操作指南（部署、开发、监控、备份/回滚/容量） | 与实际操作流程保持一致，流程变更即更新 |
| `design/` | 设计文档（对标 RFC），一文一事 | 遵循「设计先行 → 确认后实施」；实施完成后回写实施记录章节并更新状态；完全落地且不再单独演进后可移入 `archive/` |
| `archive/` | 归档（一次性报告、已完结文档） | 只读封存，不再更新；文件名可带日期后缀 |

**命名**：小写 kebab-case，见名知义（如 `env-isolation-and-workflow-refactor.md`）。

## 写作约定（2026-11-08 定，适用于除 `archive/` 外的全部文档）

这三条不是文风问题，是**这个项目真实翻过车的地方**：一份文档里同时有实测数字和没量过的判断，
读者没法区分，最后连实测的那部分也不被信了。

### 1. 实测数字必须带口径

写「恢复 22s」时，必须能指出这是怎么量的：命令、报告链接，或同段落里说明样本规模。
没有口径的数字等于没写——它不能复现，也不能用来判断"是不是变慢了"。

- ✅ `PG 915MB/60 万行 → 备份 46s、恢复 22s（scripts/scale_rto_drill.sh MODE=pg，结果见 docs/benchmarks/rto-scale-2026-11-05.md）`
- ❌ `恢复很快，几十秒`

### 2. 判断句要么给口径，要么标「（未测）」

性能/容量/规模类判断，**不许**用「可接受 / 性能足够 / 不会成为瓶颈 / 足够快」这类词
代替测量。确实只能估的，写成：

> 单用户 1 万会话下的列表响应（未测）：预计在 100ms 量级，依据是索引覆盖了 `(user_id, updated_at)`。

标「（未测）」不是免责声明，是告诉读者这条结论还需要什么才能成立。

`scripts/check_docs_claims.py` 会扫这几个词（只扫结论类文档，`archive/` 与 `audit/` 除外），
CI 里在 `lint` job 第一步执行。词表刻意很短：误报多了就没人看，反而更糟。

### 3. 「完成」= 有可执行的验收

写「已实现 / 已修复」时，要能指出验收方式：一条测试、一个脚本、一次实测。
只有"看起来能跑"的，写「已接上（未验证）」。

**CI 覆盖边界**：CI 跑 lint / 单测 / 集成 / 构建；**演练脚本与 `/health/e2e` 不在 CI 里**
（托管 runner 起不了完整栈）。CI 绿不等于演练过——反过来，演练失败不会让 CI 变红，
这是刻意的取舍，写在 [development.md](guide/development.md) 与 README 的验证段。

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
├── FINAL-REPORT.md      # ★ 结项说明（2026-11-07）：是什么 / 做到了什么 / 没做什么 / 遗留风险
├── project-status.md    # 已归档跳转页 —— 全文见 archive/project-status-2026-11-07.md（417 行台账，冻结）
├── e2e-verification-2026-11-03.md # 端到端真机验证报告（10 个真实问题、4 好/2 偏/4 差、两个缺陷）
├── articles/            # 对外可读的长文（叙事性，非规范文档）
│   └── silent-failures.md   #   删除可对账 / 失败可判定 / 断连可续（参考实现的技术叙事）
├── audit/               # 对抗性检查与审计报告（假设声称是假的，去证伪）
│   ├── adversarial-2026-11-07.md  # 结项后对抗检查：6 项不成立 + 8 项不足与方案（含 sessions 索引缺失）
│   └── plan-2026-11-07.md         # 8 项不足的整改排期（A/B/C 三批 + 可穿插的工程债）
├── benchmarks/          # 评测与实测数据（结论 + 可复现的 JSON）
│   ├── quality-and-cost-2026-11-04.md  # 汇总页：四份评测收成一页，含「不显著不写成结论」的口径
│   ├── retrieval-baseline-2026-11-02.md    # 90 题合成集基线（hit@5 0.869 / MRR@5 0.739）
│   ├── channel-ab-2026-11-02.md            # 通道 A/B（10 个臂）
│   ├── channel-fusion-2026-11-02.md        # 融合策略 + McNemar 显著性
│   ├── stage-probe-2026-11-03.md           # 真实文档逐级探针：概览题丢在哪一级（24 题 / 6 个修法）
│   ├── rto-scale-2026-11-05.md             # 真实规模 RTO：PG 915MB/22s、Milvus 2 万向量/29~33s、MinIO 195MB/18s
│   └── perf-baseline.md                    # 性能基线与夜间回归：量什么、怎么判、红了怎么查（附 perf-baseline.json）
├── guide/               # 操作指南
│   ├── deployment.md    #   部署（开发/生产环境）
│   ├── configuration.md #   配置（env 文件加载机制、全量参数、端口）
│   ├── development.md   #   开发与测试（含踩坑记录）
│   ├── fault-injection.md #  故障注入演练：三类注入、判据、六次假通过的复盘
│   ├── monitoring.md    #   监控运维（指标、告警、处置）
│   ├── quickstart-verification-2026-11-04.md # 干净目录复现验证记录（7 轮，找出 7 处问题）
│   └── backup-restore.md #  备份 / 恢复 / 回滚 / 容量与演练（运维 Runbook）
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
    ├── self-check-2026-11-04.md    # 项目自检报告（收尾后对抗性核对；查出 CI 红、测试未隔离、生产无管理员）
    ├── project-status-2026-11-07.md # 项目状态台账全文（417 行，P0-1~P2-3 全部条目 + DEPLOY 记录 + 红线表），结项冻结
    ├── README-dev-2026-11-07.md    # 旧版开发向 README 全文（三种启动方式逐步说明、脚本参数、项目结构树）
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
| [security-multitenancy-hardening-2026-10.md](design/security-multitenancy-hardening-2026-10.md) | 已实施 | 安全与多租户加固（2026-10-24 对抗性审查）：P1×5、P2×10、P3×6 全部完成（2026-10-27，实施记录见 §10/§11）；公网多租户仍受 TLS/HSTS 与最小权限边界约束。任务台账见 [project-status.md](project-status.md)「安全与多租户加固专项」 |
| [ui-visual-polish-2026-10.md](design/ui-visual-polish-2026-10.md) | 已实施 | 前端视觉收口（VIS-3，2026-10-24）：8px 网格/768px 聊天主列、EP 弹窗/下拉/表单/表格 token 化、侧栏与知识库/设置页密度收口；浅深色与移动端截图验证 |
| [model-provider-registry-2026-10.md](design/model-provider-registry-2026-10.md) | 部分实施 | 模型 Provider/Model/Route 三层：P1/P2/P2.1/P3 已完成（2026-10-26）；含熔断、用量/成本持久化、设置页展示、Embedding 影子集合重建/回滚与可选 rerank 路由；剩余 P4（Anthropic/Gemini/Azure、租户/成本策略） |
| [evolution-roadmap-2026-10.md](design/evolution-roadmap-2026-10.md) | 已确认 | 全项目演进与完善路线（2026-10-22 审计）：业界「标配/差异化/避免」对标 + 代码/UX/视觉/运维差距证据 + 三批次执行序；任务条目见 [project-status.md](project-status.md) §二「全项目完善与演进专项」 |
| [evolution-research-2026-09.md](design/evolution-research-2026-09.md) | 已确认 | 演进方向调研（2026-09-23）：从 RAG 问答到自进化知识运行时，四阶段路线于 2026-09-29 owner 拍板转正，立项见 special-projects-2026-09.md |
| [wiki-navigable-workspace.md](design/wiki-navigable-workspace.md) | 已实施 | 阶段一：Wiki 升级为 Agent 可导航工作空间（D1/D3/D4 落地；D2/D5 经 A/B 证伪后回退默认值，见 §10） |
| [special-projects-2026-09.md](design/special-projects-2026-09.md) | 部分实施 | 技术债与演进专项立项 A~E；C / D / E.6 已完成；E-1 的 W1/W2/W3 与 Agent e2e 验收已完成（无增益，W1 保持默认关闭，见 e1-navigable-lookup-gap.md）；A/B 按触发器挂起 |
| [e1-navigable-lookup-gap.md](design/e1-navigable-lookup-gap.md) | 已实施 | E-1 导航式 wiki_lookup：W1/W2/W3 已实施（2026-10-15）；E1V 离线两轮修编译覆盖后 C 臂 0.188→0.938；**Agent e2e 已于 2026-10-21 完成：无增益，W1 保持默认关闭** |
| [wiki-link-metadata-edges.md](design/wiki-link-metadata-edges.md) | 部分实施 | Wiki 图边「仅元数据建边」（L1 已实施 + 编译覆盖修复 + 离线验证，2026-10-20）；Agent e2e 已完成且无增益，L2 暂缓 |
| [learning-loop-wiring.md](design/learning-loop-wiring.md) | 已实施 | 学习闭环接入（LEARN）：W-A 权重回载 / W-B 样本补采 / W-C A/B 接入 chat（2026-10-16 单测 + dev 端到端验证通过），三开关灰度默认关 |
| [prod-schema-drift-repair.md](design/prod-schema-drift-repair.md) | 已实施 | 生产库 schema 漂移修复：代码侧（幂等对账迁移 + 去 start-prod stamp + CI `alembic check` 卡点）+ prod 窗口执行（`upgrade head` 后 `alembic check` 无漂移）全部完成（2026-10-19） |
| [fix-plan-2026-09-24.md](archive/fix-plan-2026-09-24.md) | 已归档 | 四份评审综合修复计划（2026-09-29 收官：W0-2 完成、W1~W5 全部完成、W6 八个批次完成，78 项 P2 全部落账）；剩余仅用户侧操作 W0-1/W0-3，见文档头部状态 |
| [project-readthrough-2026-09-30.md](archive/project-readthrough-2026-09-30.md) | 已归档 | 项目通读报告（2026-09-30 只读代码审查） |
| [direct-model-switch-plan-2026-10-03.md](archive/direct-model-switch-plan-2026-10-03.md) | 已归档 | 直答模型切换与 A/B 验证执行方案（2026-10-03 定稿；2026-10-15 移入 archive/ 封存） |

## 按场景查找

| 场景 | 阅读顺序 |
|---|---|
| 新人上手 | 根 [README](../README.md) → [FINAL-REPORT.md](FINAL-REPORT.md)（先看哪些没做）→ [architecture.md](architecture.md) → [guide/deployment.md](guide/deployment.md) |
| 本地开发/跑测试 | [guide/development.md](guide/development.md) |
| 排查线上问题 | [guide/monitoring.md](guide/monitoring.md) → [guide/development.md](guide/development.md) 踩坑记录 |
| 备份/恢复/回滚/扩容 | [guide/backup-restore.md](guide/backup-restore.md)（RPO/RTO、恢复顺序、回滚矩阵、容量模型与演练） |
| 查接口 | [api.md](api.md)（另有 Swagger UI: `http://localhost:8000/docs`） |
| 配置/切换模型 | 设置 → 模型服务（`/settings/models`）→ [design/model-provider-registry-2026-10.md](design/model-provider-registry-2026-10.md) → [guide/deployment.md](guide/deployment.md) §6.3.2 |
| 了解技术决策/规划 | [FINAL-REPORT.md](FINAL-REPORT.md) §3（没做什么与原因）→ [design/special-projects-2026-09.md](design/special-projects-2026-09.md) → [design/improvement-roadmap.md](design/improvement-roadmap.md) |
| 跟进实施状态 | [archive/project-status-2026-11-07.md](archive/project-status-2026-11-07.md)（已冻结的台账全文）→ 本文件「design/ 文档状态表」→ 对应文档的实施记录章节 |
| 公网上线/安全评估 | [security-multitenancy-hardening-2026-10.md](design/security-multitenancy-hardening-2026-10.md) → [archive/project-status-2026-11-07.md](archive/project-status-2026-11-07.md)「安全与多租户加固专项」→ [agent-guardrails-hitl.md](design/agent-guardrails-hitl.md) |
