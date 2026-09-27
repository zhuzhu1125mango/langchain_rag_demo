# 阶段一实施设计：Wiki 升级为 Agent 可导航工作空间 + 答案回流（2026-09-23）

> 状态：已实施（D1/D3/D4 代码与单测落地；D2/D5 经 A/B 证伪后回退默认值，见 §10）
> 上游文档：[evolution-research-2026-09.md](evolution-research-2026-09.md)（调研与四阶段路线）
> 实证依据：`scripts/run_wiki_multihop_ab.py` A/B（混入式 recall -0.062，缺口在"导航"而非"压缩"）

## 0. 目标与不做什么

**目标**：把 Wiki 编译层从「被动混入检索的语料」升级为「Agent 可导航、可回流、可自检的工作空间」，以 Karpathy Pattern 的 query/ingest/lint 三工作流补齐跨文档桥接与自进化能力。

**不做**（明确排除，防蔓延）：完整 GraphRAG（实体抽取建图）、社区摘要、向量库 schema 变更、前端大改版。

## 1. 现状盘点（设计依据）

| 组件 | 现状 | 本设计如何用 |
|---|---|---|
| `wiki_pages` 表 | page_type(entity/topic/index)、links(JSONB)、content_path(MinIO)、(kb_id,page_type,title) 幂等 | 新增 `synthesis` 页型（String(16) 无需迁移）；links 即导航边 |
| `wiki_lookup_tool.py`（Agent 工具） | 仅按 query 检索 source_kind="wiki" 切片，无导航 | **D1 升级**：支持按页取读 + 透出 Related 链接 |
| `agent_orchestrator.py:346-356` | 自动为 kb_search/wiki_lookup 注入会话 kb_ids | 导航升级对两条 Agent 路径同时生效，零额外接线 |
| `wiki_link_expansion.py` | 检索管线内"命中 wiki 页→顺 links 追加邻居块"（每页 2 块/上限 6 块，不挤占原命中），开关默认关 | **D2 打开默认**，作为非 Agent 路径的桥接 |
| `wiki_compiler.py` | compile_pages / regenerate_page / index 页重建、(kb_id,page_type,title) upsert、事实保留率探针、矛盾检查 | **D3 复用** compile 管线生成 synthesis 页 |
| `wiki_compile_scheduler.py` | per-KB 进程内去抖队列 | **D3 复用**合成页去抖 |
| `wiki_lint.py` + `GET /api/wiki/lint` | 手动触发 lint | **D4 增加周期调度** |
| 多跳 A/B | A 臂(原始) recall 0.938 / B 臂(+混入) 0.875 | **D5 新增 D 臂**（入口+链接导航）验证修复 |

## 2. D1 导航式 wiki_lookup（核心）

**工具签名升级**（向后兼容，`query` 语义不变）：

```
wiki_lookup(query, page_title?, depth?)
- query 无 page_title：入口检索（现行为）→ 结果末尾追加
  "Related: [[页A]] [[页B]] …"（来自命中页的 links 字段）
- page_title 提供时：按 (kb_id, normalize_title(page_title)) 精确取页，
  从 MinIO content_path 读全文（截断 WIKI_NAV_PAGE_MAX_CHARS=4000），
  输出含页内 links → Agent 可继续沿链接取读（渐进式披露）
```

实现要点：
- 新服务方法 `src/services/wiki_navigator.py`：`search_entries(query, kb_ids)`（复用 KBRetrievalService，source_kind="wiki"）+ `get_page_by_title(kb_id, title)`（查 wiki_pages 表 → MinIO 读正文，fail-soft）。
- `WikiLookupTool` 注入 navigator；parameters schema 增加 `page_title`；输出文本统一带 Related 行（Agent 循环自然可多轮调用；受既有 agent max_steps/时间预算约束，无需新增长度控制）。
- LLM 提示词侧无需改动：工具 description 更新为"可沿 Related 链接继续取页"。

**验收**：mock 单测覆盖入口→取页→沿链接二次取读三步；`looks_like_tool_call` 不误伤新输出格式。

## 3. D2 检索管线：入口 + 链接扩展成为默认

- `WIKI_LINK_EXPANSION` 默认值 false → **true**（A/B 证据：追加式扩展不挤占原命中，直接补跨文档桥；原始混入行为不变，另做检索侧 A/B 再议是否缩减混入比例——本设计不混入改动）。
- `expand_wiki_links` 小修：扩展块数量上限与 `WIKI_NAV` 相关常量收敛到 `WikiSettings`，可配置。

## 4. D3 答案回流（synthesis 页，自进化核心）

**触发**（`rag_chain._finalize_side_effects` 内，best-effort，异常只告警）：

```
条件（全部满足）：
- WIKI_SYNTHESIS_ENABLED=true（新增，默认 false，灰度）
- answer_type ∈ {knowledge_base, hybrid_search}，无 web 来源
- len(final_answer) ≥ WIKI_SYNTHESIS_MIN_ANSWER_CHARS（默认 200）
- state.docs 引用了 wiki 页 或 命中 ≥2 个源文档（单文档事实不值得合成）
- 语义去重：与该 KB 既有 synthesis 页 embedding 相似度 < 0.92（复用语义缓存阈值口径）
动作：
- 组装 material = 问题 + 最终答案 + 引用 chunk 原文（截断 WIKI_REWRITE_MATERIAL_CHARS 口径）
- 经 wiki_compile_scheduler 去抖（复用 WIKI_COMPILE_DEBOUNCE_SECONDS）批量合成
- WikiCompiler 新增 compile_synthesis(material, cited_doc_ids, source_page_ids)：
  LLM 产出/合并 synthesis 页（page_type="synthesis"），
  links 指向引用涉及的 entity/topic 页，source_doc_ids=引用文档
- index 页重建纳入 synthesis 分节；lint 对新页自动执行矛盾检查（编译器已有）
```

数据与治理：
- 页 frontmatter 记录 origin: `question` / `created_from: chat` / `confidence`（事实保留率探针值），保证 provenance（阶段三治理的地基）。
- synthesis 页**不混入主检索**（保持 source_kind="wiki" 但生成时写入 metadata `no_raw_mix=true`，检索入口定位仍可用）——避免重蹈"挤占"覆辙，待 D 臂 A/B 数据再定。
- 去抖队列沿用 per-KB 进程内实现（多副本局限已在架构文档声明，不扩大范围）。

**验收**：同一问题二次提问命中缓存/synthesis 页；`GET /api/wiki/pages?page_type=synthesis` 可列出；全链路异常不阻断聊天（单测覆盖 5 类失败注入）。

## 5. D4 Lint 常态化

- lifespan 启动后台任务：每 `WIKI_LINT_INTERVAL_HOURS`（默认 24，0=关）对有 active wiki 页的 KB 跑 `lint_kb_wiki`，结果记日志 + 经既有 notification 服务推送（可选 `WIKI_LINT_NOTIFY=false` 默认关）。
- 手动 `GET /api/wiki/lint` 保持不变。

## 6. D5 证据闭环：多跳 A/B 增加 D 臂（导航式）

`run_wiki_multihop_ab.py` 增加 D 臂：**入口检索（仅 wiki 页）→ 顺 links 一跳取邻居页 → provenance 计 recall**（离线模拟 Agent 导航，复用 `wiki_pages.links`）。判定标准：
- D ≥ A(0.938) 且 D > B(0.875) → 导航式修复跨文档缺口成立，阶段一上线依据充分；
- D ≤ B → 设计证伪，回滚 WIKI_LINK_EXPANSION 默认值并重新评审。

## 7. 配置变更（env-consistency CI 必须三处同步）

| 键 | 默认 | 说明 |
|---|---|---|
| `WIKI_LINK_EXPANSION` | false → **true** | 既有键，改默认值 |
| `WIKI_SYNTHESIS_ENABLED` | false | 答案回流灰度开关 |
| `WIKI_SYNTHESIS_MIN_ANSWER_CHARS` | 200 | 回流最短答案 |
| `WIKI_LINT_INTERVAL_HOURS` | 24（0=关） | lint 周期 |
| `WIKI_NAV_PAGE_MAX_CHARS` | 4000 | 导航取页截断 |

同步位置：`config.py(WikiSettings)` / `.env.example` / `.env.dev` / 两份 compose env_file 注释（prod 不启用 synthesis，保持默认关）。

## 8. 文件变更清单

| 文件 | 变更 |
|---|---|
| `src/services/wiki_navigator.py` | **新增**：入口检索 + 按页取读 + Related 解析 |
| `src/services/tools/plugins/wiki_lookup_tool.py` | 升级：page_title 参数 + Related 输出 |
| `src/services/wiki_compiler.py` | 新增 compile_synthesis；index 页重建纳入 synthesis |
| `src/services/wiki_synthesis_service.py` | **新增**：触发条件判定 + 语义去重 + 去抖接入 |
| `src/services/pipeline/stages.py` | `_finalize_side_effects` 接入回流（best-effort） |
| `src/services/wiki_lint.py`（或新 scheduler） | 周期调度任务 |
| `src/config.py` + 4 个 env 文件 | 上表 5 个配置项 |
| `scripts/run_wiki_multihop_ab.py` | 增加 D 臂 |
| `tests/test_wiki_navigator.py`、`tests/test_wiki_synthesis.py` | 新增单测 |
| `tests/evaluation/test_wiki_multihop_ab.py` | D 臂结构测试 |
| `frontend`（可选，最后做） | WikiDrawer 增加 synthesis 页型徽标 |

## 9. 实施顺序与工作量

1. D1 导航工具 + 单测（半天）
2. D2 开关默认值 + D5 D 臂 A/B 跑通（1 小时）——**先拿证据**
3. D3 回流服务 + 管线接入 + 单测（1 天）
4. D4 lint 调度（半天）
5. 全量回归 + `.env` 三处同步 + 文档回写（roadmap 状态、architecture.md）

## 10. 实施记录与 D 臂证伪结论（2026-09-23）

**已落地**：D1（`wiki_navigator.py` + 导航式 wiki_lookup，7 单测）、D3（`wiki_synthesis_service.py` + 编译器 `compile_synthesis_page`/synthesis 页型/主检索排除 wiki_syn + 管线终态钩子，6 单测）、D4（`wiki_lint_scheduler.py` + lifespan 挂载，3 单测）；配置 5 键入 config/.env.example/.env.dev（synthesis 默认关灰度）。

**D2/D5 两轮 A/B（数据集 8 个跨文档问题，编译模型 qwen3:4b-instruct-2507-q4_K_M）**：

| 臂 | 第一轮（初始实现） | 第二轮（互链 prompt 修复后） |
|---|---|---|
| A 仅原始 chunk（基线） | recall@5=0.938 | 0.938 |
| B 原始 chunk+wiki 混入 | 0.875 | **0.812**（互链文本稀释 BM25） |
| C 仅 wiki 页（诊断） | 0.875 | **0.750** |
| D 导航式（入口+一跳链接） | **0.625** | **0.562** |

过程与结论：
1. 第一轮 D=0.625，诊断发现**编译页正文 0 条 [[互链]]**（生成 prompt 未要求互链，链接图为空）——"桥没建"。
2. 互链 prompt 修复（注入既有页标题、要求 [[互链]]）后链接图 57 条，但 D 反而降至 0.562，且 B/C 同步退化：链接文本混入正文稀释检索，5-slot 内邻居页挤占第二篇 golden 文档。
3. **按 §9 既定判据执行回退**：`WIKI_LINK_EXPANSION` 维持 false（机制保留可配置启用）；互链 prompt 改动整体回退（B/C 恢复已知基线 0.875）；D1/D3/D4 代码保留（独立价值，均灰度默认关）。

**教训与重评审条件**：
- 5-slot 被动检索框架原理上无法体现导航价值——Agent 多轮取页不受 slot 限制，但离线代理测不到该形态；现有证据下不应为它牺牲主检索质量。
- 跨文档桥的正确建法是**结构化方案**（独立链接元数据/图结构，与页面正文分离），而非 prompt 生成的内联文本；需重新设计后再评审。
- 重启触发条件：真实 trace 中跨文档问题占比可观，且 Agent 多轮导航收益有真实测量手段（如 wiki_lookup 多轮调用的 trace 埋点）。

**最终回归**：全量 `916 passed / 103 skipped`（排除 `test_structured_chunking.py`——该文件单独运行 7 passed 全过；其 Word 解析用例在长进程套件中因 Windows python-magic-bin 旧版 libmagic DLL 非确定性崩溃/挂起，属环境问题，与本次改动无关，单跑稳定）。仍有 2 个**既有失败**（`test_metrics_reset` 401、`test_invalid_jwt_dev_mode_falls_back_to_default`），已在未改动基线上复现，属 2bdfec8 安全加固后测试未同步，另行修复。
