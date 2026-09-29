# 文档索引

> 命名与组织方式参考 Django / Vue / FastAPI 等项目惯例：核心参考置于顶层，其余按性质分目录；文件名使用小写 kebab-case。

## 目录与命名规则

| 目录 | 职责 | 归档/流转规则 |
|---|---|---|
| 顶层（`architecture.md`、`api.md`） | 随代码持续维护的核心参考 | 代码结构或接口变更时必须同步更新 |
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
│   ├── evolution-research-2026-09.md
│   └── wiki-navigable-workspace.md
└── archive/             # 归档（一次性报告、已收官计划）
    ├── code-review-2026-08.md      # 代码审查报告（已全部修复）
    ├── code-review-2026-09.md      # 代码审查报告（4 项阻断问题已修复；文档漂移已全量回写）
    ├── code-review-2026-09-23.md   # 代码审查报告（P0×1 + P1×5 + P2×17；P1 已修复入库）
    ├── code-review-2026-09-24.md   # 代码审查报告（已全部修复：W1~W6 全部落地）
    └── fix-plan-2026-09-24.md      # 四份评审综合修复计划（2026-09-29 收官，78 项 P2 全部落账）
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
| [chat-latency-and-model-config.md](design/chat-latency-and-model-config.md) | 已实施 | 首字延迟治理 + 模型配置去硬编码（2026-09-07），记录见 §10 |
| [frontend-test-plan.md](design/frontend-test-plan.md) | 已实施 | 前端单测 + E2E 体系（P2-6，2026-09-06） |
| [semantic-cache.md](design/semantic-cache.md) | 已实施 | 语义缓存 P1-3（2026-09-07），记录见 §8 |
| [severe-fixes.md](design/severe-fixes.md) | 已实施 | 严重问题 1~9 修复；来源报告归档于 archive/code-review-2026-08.md |
| [agent-evolution.md](design/agent-evolution.md) | 部分实施 | Agent 有界循环 Phase 1/2 已落地，L1-a 记忆已实施（见 archive/code-review-2026-09.md §6.5） |
| [agent-ab-evaluation.md](design/agent-ab-evaluation.md) | 已执行验收 | A/B 两臂 live 对比完成（2026-09-14），结果与未达标项见 §7.1 |
| [evolution-research-2026-09.md](design/evolution-research-2026-09.md) | 草稿 | 演进方向调研（2026-09-23）：从 RAG 问答到自进化知识运行时，含四阶段路线；待确认后实施 |
| [wiki-navigable-workspace.md](design/wiki-navigable-workspace.md) | 已实施 | 阶段一：Wiki 升级为 Agent 可导航工作空间（D1/D3/D4 落地；D2/D5 经 A/B 证伪后回退默认值，见 §10） |
| [fix-plan-2026-09-24.md](archive/fix-plan-2026-09-24.md) | 已归档 | 四份评审综合修复计划（2026-09-29 收官：W0-2 完成、W1~W5 全部完成、W6 八个批次完成，78 项 P2 全部落账）；剩余仅用户侧操作 W0-1/W0-3，见文档头部状态 |

## 按场景查找

| 场景 | 阅读顺序 |
|---|---|
| 新人上手 | 根 [README](../README.md) → [architecture.md](architecture.md) → [guide/deployment.md](guide/deployment.md) |
| 本地开发/跑测试 | [guide/development.md](guide/development.md) |
| 排查线上问题 | [guide/monitoring.md](guide/monitoring.md) → [guide/development.md](guide/development.md) 踩坑记录 |
| 查接口 | [api.md](api.md)（另有 Swagger UI: `http://localhost:8000/docs`） |
| 了解技术决策/规划 | [design/improvement-roadmap.md](design/improvement-roadmap.md) → `design/` 其他文档（状态见上表） |
| 跟进实施状态 | 本文件「design/ 文档状态表」→ 对应文档的实施记录章节 |
