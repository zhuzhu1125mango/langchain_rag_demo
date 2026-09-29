# 技术债与演进专项立项（2026-09）

> 状态：立项草案（待 owner 逐项确认后启动；遵循「设计先行 → 确认后实施」）
> 日期：2026-09-29
> 来源：[code-review-2026-09-24.md](../archive/code-review-2026-09-24.md) §4 P2 表（#5/#46/#47/#51/#54）+ [fix-plan-2026-09-24.md](../archive/fix-plan-2026-09-24.md) W6 收官遗留决策（#5 决策渐进式、#46/#47 留规模触发）+ [evolution-research-2026-09.md](evolution-research-2026-09.md)（草稿待评审）+ [agent-evolution.md](agent-evolution.md) §10.6/§10.7/§11.6（L3/L4 方向保留）
> 现状核实：2026-09-29 以代码实测为准。与评审报告数字有出入处，已在各专项「现状核实」标注修正。

---

## 0. 专项总览

| # | 专项 | 来源 | 执行模式 | 优先级 | 启动条件 | 依赖 |
|---|---|---|---|---|---|---|
| A | 巨型文件拆分（document.py / web_search_service.py） | 0924 评审 #5 | 渐进式（触碰即拆） | 中 | 触碰对应模块的需求/缺陷进入时 | 无 |
| B | 前端性能触发项（消息虚拟化 #46 + 图谱 Canvas #47） | 0924 评审 #46/#47 | 规模触发 | 低 | 消息/节点量级达阈值（见 §B.2） | 无 |
| C | 类型全面严格化 | 0924 评审 #51/#54/#56/#61 | 专项迭代 | 中 | 下一前端专项迭代窗口 | 无（与 D 并行可互利） |
| D | 覆盖率阈值收紧 | 0924 评审 #63/#65 | 立即启动 + 持续上调 | 高 | **立即**（前端 CI 覆盖率卡点缺失，属「配置存在但从未生效」） | 无 |
| E | 自进化知识运行时（四阶段）+ Agent L3/L4 | evolution 草稿 / agent-evolution §10 | 战略路线（✅ 已拍板转正，见 §E.5） | 高 | 阶段一于专项 D 完成后启动 | 阶段一复用既有 wiki 管线与 A/B 框架 |

**确认后的执行顺序（2026-09-29 owner 拍板）**：D（立即）→ C（与 D 交错）→ **E 阶段一（D 完成后启动）**；L3 critic 反思独立灰度（建议 D 后与阶段一并行排期，时点实施时定）；A/B 按触发器挂起。

---

## 专项 A：巨型文件拆分（#5，渐进式）

### A.1 现状核实（2026-09-29 实测，修正评审数字）

| 文件 | 评审报告数字 | 实测 | 结论 |
|---|---|---|---|
| `backend/src/services/rag_chain.py` | ~2029 行 | **329 行** | **已达标，移出拆分清单**（此前批次已拆分/去重）。仅留守卫注记：新增职责不得回灌单文件 |
| `backend/src/api/document.py` | ~1674 行 | **1679 行** | 待拆（主债） |
| `backend/src/services/web_search_service.py` | ~996 行 | **1039 行** | 待拆（次债） |
| `backend/src/api/chat.py` | stream_answer 300+ 行 | 863 行；`stream_answer` L261-545 约 285 行 | W6 #4 已部分实施（`_prepare_stream_session` 已抽出），降级为「触碰时顺手项」 |

### A.2 document.py 拆分预案（1679 行 → 子包）

目标结构：`backend/src/api/documents/` 包，**保留 `api/document.py` 为薄聚合壳**（re-export router 与公共名），`main.py` 挂载点与全部调用方 import 路径零改动。

| 新模块 | 内容（当前行号） |
|---|---|
| `schemas.py` | Pydantic 模型：DocumentResponse/DocumentUpdate/BatchDeleteRequest/DocumentSourceResponse（L118-195）、SearchResult（L811）、DuplicateDetectionResult（L1520） |
| `processing.py` | 后台任务：`process_document_async`（L227-471，约 244 行）、`process_document_delete_async`（L471-546） |
| `upload.py` | `upload_document`（L546-665）、`batch_upload`（L665-755）、上传进度/WS（L1467-1520） |
| `query.py` | `search_documents`（L823-925）、`list_documents`（L925-1010）、详情/preview/chunks/source（L1025-1138） |
| `lifecycle.py` | reprocess/classify/evaluate/get/update/delete/batch_delete（L1138-1467） |
| `duplicates.py` | `detect_duplicates`（L1530-1620）、`analyze_document_with_llm`（L1620）、`calculate_similarity`（L1639） |
| `router.py` | 所有 `@router` 端点声明集中于此，各业务模块只含函数实现 |

依赖方向单向：`router → 业务模块 → schemas`，禁止反向引用；后台任务（processing）不得 import 路由模块。

### A.3 web_search_service.py 拆分预案（1039 行 → 子包）

目标结构：`backend/src/services/web_search/` 包，原 `web_search_service.py` 保留为 re-export 壳（grep 确认引用方后逐步收敛 import）。

| 新模块 | 内容（当前行号） |
|---|---|
| `models.py` | `WebContent`（L118）、`WebSearchError`（L128） |
| `query_rewriter.py` | `SearchQueryRewriter`（L143-277） |
| `reranker.py` | `WebReranker`（L277-410，含模型加载单例） |
| `engines.py` | tavily（L616）/ searxng（L579）/ duckduckgo（L645）三引擎实现 |
| `fetcher.py` | 抓取与抽取（L677-843），**含 `_fetch_html` 逐跳 GET + 每跳 `validate_url_safe`（SSRF 防护，拆分时行为一字不改，测试锁定）** |
| `service.py` | `WebSearchService` 门面 + `build_search_context_*`（L843-1039） |

### A.4 触发器与守卫（渐进式约定）

- **触发即拆**：任何落在 document.py / web_search_service.py 的需求或缺陷，先提交一次「纯拆分 PR」（只移动 + re-export，零逻辑改动），再在该 PR 之上做功能改动。单次 PR 只拆一个文件。
- chat.py `stream_answer`（~285 行）降级为触碰时顺手项：下次改聊天链路时按 `_prepare_session / _generate_sse / _persist_assistant_message` 三段收尾拆分。
- 拆分 PR 禁止顺手重构、禁止修改任何函数签名与行为。

### A.5 验收标准

- 拆分后单文件 <600 行；现有测试仅允许 import 路径调整，**零断言改动**即通过（行为等价证明）。
- 后端全量回归基线：976 passed / 102 skipped；CI 五 job 全绿；覆盖率不低于拆分前。
- grep 确认无重复实现残留、无循环导入（`uv run python -c "import src.main"` 冒烟）。

### A.6 风险与回退

- 循环导入：schemas 层零依赖可规避；拆分 PR 内先画依赖草图再动手。
- 后台任务模块归属易错（processing 依赖进度 WS/进度管理器）：归入 `api/documents/processing.py` 而非 services，保持与现有 P1-9「后台任务独立会话」教训一致。
- 回退：纯移动 PR 直接 revert 即可，无数据/接口变更。

---

## 专项 B：前端性能触发项（#46 消息虚拟化 / #47 图谱 Canvas）

### B.1 现状核实

- `frontend/src/components/chat/ChatMessageList.vue`（270 行）：普通 `v-for`（L22-133）全量渲染，无虚拟化；自动滚动 scrollToBottom（L261-267，近底 50px 阈值）。子组件：MarkdownRenderer、MessageSources、ReasoningPanel、ThinkingPanel。
- `frontend/src/components/KnowledgeGraph.vue`（511 行）：纯 SVG 渲染（L42-133），自定义分层布局 `calculateNodePositions`（L300-352，知识库顶行 + 文档按 kb_id 分组），**无缩放/拖拽**，仅节点点击选中与容器 resize 重算。

### B.2 触发阈值（达到任一即启动对应子项）

| 子项 | 触发条件（可观测口径） |
|---|---|
| #46 消息虚拟化 | 单会话消息 >200 条，或会话切换/滚动出现可感知卡顿（>100ms 掉帧）；以真实使用体验为准，不凭空预优化 |
| #47 图谱 Canvas | 单图节点 >300，或 SVG 重渲染进入 16ms 帧预算困难（DevTools Performance 实测） |

### B.3 方案预案

**#46 虚拟化（混合模式）**：
- 仅对「历史长会话」启用虚拟列表（`vue-virtual-scroller` DynamicScroller，MIT 开源离线可用）；当前活跃流式会话保持普通渲染——避免虚拟化与 SSE 增量更新、动态高度（Markdown/代码块/图片）的复杂交互。
- 关键点：流式锚底自动滚动需在虚拟化下重实现（保留 50px 近底阈值语义）；引用溯源弹窗、消息级错误卡片（W6 #41）渲染不受影响。

**#47 两步走（先低成本后重写）**：
- 第一步（触发后先做）：布局 memoization——节点/关系不变时跳过 `calculateNodePositions` 重算，仅边样式更新；顺带补缩放（滚轮）与画布平移（拖拽），当前完全缺失。
- 第二步（量级达 Canvas 触发线）：Canvas 2D 重写渲染层，自实现命中检测（节点数 <1000 无需 WebGL）；布局算法复用现有分层逻辑。不引入大型图库（G6 等离线虽可用但 bundle 成本高，除非第一步后仍不达标）。

### B.4 验收标准

- #46：长会话（≥300 条 mock 消息）切换与滚动 60fps；流式回答锚底行为与现状一致；54 单测 + 5 E2E 回归全过。
- #47：300 节点拖拽/缩放不冻结；节点点击选中语义不变；布局结果与 SVG 版本视觉一致（截图对比）。

### B.5 风险

- 虚拟化与流式渲染交互是主要复杂度来源——混合模式（历史虚拟化 + 活跃会话普通渲染）是降险关键决策。
- Canvas 重写损失可访问性（DOM 文本不可选中/搜索）——图谱页非文档阅读页，可接受，立项时明示。

---

## 专项 C：类型全面严格化（#51/#54，专项迭代）

### C.1 现状核实（远好于评审报告描述，范围收缩）

| 指标 | 评审报告推断 | 2026-09-29 实测 |
|---|---|---|
| `: any` / `as any` | 大量 | **0 处** |
| `as unknown as` | #53 一处 | 1 处（`queries/kb.ts`） |
| `Record<string, unknown>` | 多处 | 20 处 / 8 文件（queries/trace.ts ×5、queries/kb.ts ×6、TraceView.vue ×3、env.d.ts ×2、utils/auth.ts、useChatStream.ts、GeneralView.vue、useNotifications.ts 各 1） |
| tsconfig strict（#57） | 未启用 | **实际已达成**：`tsconfig.app.json` extends `@vue/tsconfig/tsconfig.dom.json`（base 含 strict）+ `noUncheckedIndexedAccess` |
| eslint no-explicit-any（#61） | 未开启 | 未开启（但 any 为 0，开启即零成本） |
| 非 `!` 断言（#54） | 滥用 | 待实测统计（实施时先出基线再定批次） |

**范围修正**：本专项从「全面严格化」收缩为「API 响应精确接口化 + lint 收紧」——任何/非空断言基数极小，无需大动干戈。

### C.2 批次

- **C1 API 响应精确接口化（#51/#53）**：为 `queries/trace.ts`、`queries/kb.ts` 的 `Record<string, unknown>` 定义精确 interface（TraceStage/TraceDetail/KB 列表项等，与后端 Pydantic schema 对齐）；消除唯一一处 `as unknown as`。
- **C2 运行时校验最小化（#52）**：**不引入 Zod**（依赖最小化原则；仅 20 处待类型化，Zod 的 bundle 与学习成本不成比例）。运行时校验仅用于真正不可信边界：SSE 消息解析、`GeneralView.vue` 的 `JSON.parse`（已有 try/catch，补 schema 化校验）。可信 axios 响应用 C1 的静态类型 + `request<T>` 泛型封装（#56）覆盖。
- **C3 eslint 收紧（#61）**：`@typescript-eslint/no-explicit-any: error`（当前 0 处，直接开）；评估追加 `no-unnecessary-type-assertion`、`no-non-null-assertion`（warn 起步，按 #54 基线决定是否升 error）。
- **C4 非空断言清理（#54）**：实施时先统计 `!` 断言基线，逐文件替换为运行时检查或默认值；仅处理真实可空场景，不为清零而清零。

### C.3 验收标准

- `vue-tsc -b` 零错误保持；eslint 新规则 0 error；`Record<string, unknown>` 在 queries 层清零（`env.d.ts` 的 2 处属类型声明层，保留豁免）。
- 54 单测 + 5 E2E 全过；无新增运行时依赖（C2 不引 Zod 为验收硬条件）。

---

## 专项 D：覆盖率阈值收紧（立即启动）

### D.1 现状核实（含两个关键事实）

- **backend**：CI 卡点存在——`ci.yml` L142 `pytest --cov=src --cov-fail-under=58`；`pyproject.toml` 无 `[tool.coverage]` 配置（阈值散落 CI 脚本，非单一事实来源）。**当前实测 60.95%，已低于 CI 注释所称「基线 61%」**。
- **frontend（关键缺口）**：`vitest.config.ts` L16-28 已配置 v8 coverage + thresholds（statements 6 / branches 5 / functions 7 / lines 6，仅全局、无 per-file，include 仅 `src/{utils,stores,composables,queries}/**/*.ts` 逻辑层）；**但 CI L349 只跑 `pnpm run test:unit`（无 `--coverage`）→ 前端覆盖率阈值在 CI 中从未生效过**。本地 W6 记录逻辑层覆盖率 11.3%。

### D.2 批次

- **D1（立即，修复卡点缺口）**：CI 前端 job 改为 `pnpm run test:unit --coverage`（或在 `test:unit` 脚本中固定）；本地先核对 11.3% 基线对 6/5/7/6 阈值通过，再入库。验收含「人为下降覆盖率可红」的反向验证。
- **D2 backend 阈值收敛与上调**：① 阈值与 omit 规则从 ci.yml 迁至 `pyproject.toml` `[tool.coverage.report] fail_under`（单一事实来源，CI 只写 `--cov`）；② 58 → **60**（当前 60.95% 留约 1 点缓冲防 CI 抖动红），补测回到 61+ 后再评估 62；③ 上调必须单向上调、禁止回退，每次调整在 CI 注释记录当日实测值。
- **D3 前端阈值上调路线**：6/5/7/6 → 10/8/12/10（对应现有 11.3% 基线留缓冲），此后每次补测批次上调 3~5 点；评估按目录设 per-module thresholds（`queries`/`stores` 先行），防止单模块拖累全局。
- **D4 补测落点（按差额排序）**：后端优先 `api/chat.py`、`api/documents`（拆分 A 前补测，为行为等价提供更强守卫）；前端优先 `useChatStream`、`useNotifications`、`queries/trace.ts`（与 C1 重叠，一次投入双收益）。

### D.3 验收标准

- CI 前端 job 输出覆盖率并按 thresholds 卡点（红/绿双向验证）。
- backend fail_under=60 生效且全绿；阈值历史调整有据可查（CI 注释记录实测值）。
- 不为凑数写断言测试：新增测试必须断言真实行为（沿用 W6 #63 useToast/kb spec 的质量标准）。

---

## 专项 E：自进化知识运行时（evolution 四阶段）+ Agent L3/L4（战略路线，待拍板）

### E.1 定位与收编关系

[evolution-research-2026-09.md](evolution-research-2026-09.md) 定调「借神不借形」：Wiki 从检索语料升级为 Agent 可导航工作空间 + Query 回流（自进化核心）。本立项确认后：

- **收编** roadmap P2-1（GraphRAG，A/B 已证无增益维持挂起）→ 降为阶段四图记忆可选件；
- **合并** roadmap P3「学习引擎闭环验收」→ 与阶段一「Query 回流」同源，一次实施；
- chunk 级权限（P3）→ 归阶段三统一入口。

### E.2 阶段一「借神」细化（确认后首个实施批次）

| 子项 | 内容 | 现状锚点 |
|---|---|---|
| E-1 导航式 wiki_lookup | 输入问题 → 读 wiki 索引/语义入口定位 → 顺 Related 链接逐页 progressive disclosure → 综合；编译页仅做入口定位，不再与原始 chunk 同池竞争 | [wiki-navigable-workspace.md](wiki-navigable-workspace.md) 已落地可导航工作区，启动时先盘点与 E-1 的能力差距 |
| E-2 Query 回流 | 有价值问答对沉淀为 synthesis 页（走既有 ingest/lint 管线 + 事实保留率探针 + 矛盾检查）；与 P3 学习引擎闭环合并 | L1-a 记忆写入通道可复用；回流阈值与触发条件需单独设计（避免噪声页污染） |
| E-3 Lint 常态化 | 定期审计矛盾/过时/孤儿页 | lint 调度已有实现（`test_wiki_lint_scheduler` 覆盖），预计仅补策略配置 |

**验收**：以跨文档多跳 A/B 复测为核心 KPI——`wiki_multihop_eval_dataset.jsonl`（此前 recall -0.062 缺口正是阶段一要修的），目标 recall 非退化且 B 臂反超 A 臂；`test_wiki_ab.py` 非退化底线保持；新增回流页走 `WIKI_CONTRADICTION_CHECK` 与事实保留率探针。

### E.3 阶段二~四概要（逐阶段再立项，不预实施）

- **阶段二 MCP 服务化**：`kb_search` / `wiki_lookup` / `web_search` 包装为 MCP server（**HTTP/SSE，局域网多用户形态**，复用 ToolManager 与检索栈）；**前置：用户级 API Key 发放/吊销（机制设计见 §E.6）**——每用户凭据 → owner 隔离与 Web 端同一套逻辑；会话记忆不进第一版暴露面（跨用户隐私面最小化，后续评估）。完全离线私有化为差异化。**验收：局域网内不同用户的 MCP 客户端各自只能查询到本人可见的知识库**（而非"任一客户端可查询全库"——多用户隔离优先于便捷分发）。部署前提：`APP_ENV=prod` 匿名放行关闭，限流/JWT 真开。
- **阶段三 Context Engine 收口**：统一检索 API（多源 + 自适应路由 + chunk 级权限 + provenance 贯通）+ wiki 页版本化/可回滚。
- **阶段四 自进化运行时**：Deep Research 长时程研究任务（产出沉淀 wiki 页）、图记忆融合（wiki 实体图 + 会话记忆 + 时序事实）、多模态按需评估。

### E.4 Agent L3/L4 与四阶段的关系（避免两套记忆通道）

- **L3 critic 反思**（agent-evolution §10.6）：SYNTHESIZE 后一次 `think=False` 校验「逐条有据」，未通过回补一次工具调用（受 max_steps+预算约束）——复用 `_get_aux_llm`，轻量，**建议独立灰度、先于阶段一**（不依赖回流设计）。
- **L3 长期记忆**：与阶段一 E-2「Query 回流」是同一「沉淀」命题的两面（会话记忆 vs 知识沉淀）。**统一设计、共用 Milvus 存储与写入通道，禁止两套沉淀管线**——具体形态在阶段一设计文档中一并给出。
- **L4 自治边界**（checkpoint/断点恢复、并行子 Agent）：维持「设计预留、运行受限」，4GB VRAM 约束不解除前不开启；checkpoint 以简单状态序列化优先，LangGraph 仅在需要完整编排拓扑时引入。

### E.5 决策记录（2026-09-29 owner 拍板）

1. **路线确认 ✅**：四阶段路线批准为正式 roadmap；[evolution-research-2026-09.md](evolution-research-2026-09.md) 状态从「草稿」转「已确认」。
2. **阶段一启动时点 ✅**：专项 D 完成后启动（覆盖率守卫先行）。
3. **L3 critic 反思 ✅**：独立灰度先行，不随阶段一捆绑；建议 D 后与阶段一并行排期。
4. **MCP 分发边界 📋 建议按 owner 反馈修正（2026-09-29，多用户形态，待最终确认）**：初版"本机/局域网个人使用"建议被 owner 否决（项目是多人使用，且 stdio + 共享机器凭据与"多用户数据隔离"理念冲突）。修正为——传输 **HTTP/SSE（局域网服务端）**而非 stdio；认证按**每用户各自凭据**（前置：用户级 API Key 发放/吊销，MCP 客户端填各自 Key → 服务端解析 user_id → owner 隔离与 Web 端一致，禁止共享机器凭据导致隔离退化）；工具面只读白名单（kb_search/wiki_lookup/web_search），会话记忆不进第一版；端点仅局域网可达，部署必须 `APP_ENV=prod` 关匿名放行。待 owner 确认后作为阶段二设计前提。**用户级 API Key 的完整机制设计（数据模型/认证链插入/发放吊销 API/验收）见 §E.6。**
5. **收编确认 ✅**：P2-1 GraphRAG 正式关闭（roadmap 已回写）；P3 学习引擎闭环并入阶段一 E-2、chunk 级权限归阶段三。

### E.6 用户级 API Key：发放与吊销机制设计（阶段二前置，独立小专项）

> 设计先行，待确认后实施。独立于 MCP 的价值：脚本/CI 等机器集成也需要"以某个用户身份"调 API，且当前全局 `API_KEY` 命中后固定返回 `user_id="api_key_user"`（auth.py L233-234），多用户共存时资源归属会串到同一伪用户——本机制同时修复该隐患。

#### E.6.1 现状锚点（2026-09-29 核实）

- 认证链 `get_current_user`（auth.py L207-252）：`X-API-Key`（全局单把，命中 → `api_key_user`）→ JWT Bearer → 开发匿名；`get_current_user_for_ws` 同构。
- `users` 表（models/user.py）极简：id/username/password_hash/created_at，无 Key 表。
- `require_owner` 按 `owner_id == current_user.user_id` 比对——**Key 只要能解析出真实 user_id，全链路隔离零改动生效**（API/WS/审计同构）。
- 审计 `record_audit`（best-effort）、自研 IP 限流 `utils/rate_limit.py`、`/auth/*` 已有注册 3 次/分、登录 5 次/分配置项惯例。

#### E.6.2 数据模型：新表 `user_api_keys`

| 列 | 类型 | 说明 |
|---|---|---|
| id | UUID PK | |
| user_id | UUID FK→users.id，索引 | 归属用户 |
| name | String(64) NOT NULL | 用途备注（必填，如 `claude-code-office`），供审计追溯 |
| key_hash | String(64) UNIQUE NOT NULL | **SHA-256(key)**——Key 为高熵随机串无字典风险，用 SHA-256 保 O(1) 索引查询；**不用 bcrypt**（每请求 ~100ms 不可接受） |
| key_prefix | String(12) | 明文前缀（`rag_` + 前 8 字符），列表识别与日志脱敏 |
| created_at / expires_at | DateTime | expires_at 可空（可选有效期） |
| last_used_at | DateTime 可空 | 泄露感知线索；**写库节流**：距上次 >60s 才更新，防每请求写 |
| revoked_at | DateTime 可空 | 软吊销标记 |

约束：每用户上限 5 把（`USER_API_KEYS_MAX_PER_USER`，配置化）；`revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())` 才有效。

#### E.6.3 Key 生成与格式

- 格式：`rag_` + `secrets.token_urlsafe(32)`（约 43 字符）；前缀 `rag_` 便于人工识别与 gitleaks 规则覆盖。
- 发放时生成明文 → 计算 SHA-256 入库 → **明文仅创建响应返回一次**（GitHub PAT 惯例），服务端任何后续接口永不返回。

#### E.6.4 认证链改动（auth.py，最小插入）

```
get_current_user 校验顺序调整为：
1. X-API-Key 匹配全局 API_KEY → api_key_user（现有单租户自托管行为不变）
2. X-API-Key 不匹配全局 → 按 SHA-256 查 user_api_keys（有效行）→ CurrentUser(user_id=key.user_id)
3. JWT Bearer → 现有逻辑
4. 开发匿名 → 现有逻辑
```

- 实现：抽出 `_resolve_api_key(key) -> Optional[CurrentUser]`，`get_current_user` 与 `get_current_user_for_ws`（WS 首帧 `api_key` 字段）共用；比较用 `hmac.compare_digest`（哈希比较）。
- **owner 隔离零改动**：Key 解析出的 user_id 即 owner_id，`require_owner` / KB 归属校验 / 文档隔离全部按现有逻辑生效；MCP 阶段直接复用 `get_current_user`，无独立鉴权面。
- 性能：key_hash 唯一索引单查；首版不加内存缓存（MCP/脚本查询频率低，且避免吊销生效延迟）；如后续需要缓存，TTL ≤60s 并在吊销语义文档化。

#### E.6.5 发放/吊销 API（挂 `/auth`，JWT 保护）

| 端点 | 说明 |
|---|---|
| `POST /auth/api-keys` | JWT 必须；body `{name, expires_at?}`；校验用户 Key 数上限；**响应一次性返回明文** `{id, name, key_prefix, key, created_at, expires_at}` |
| `GET /auth/api-keys` | 列当前用户 Key：`id/name/key_prefix/created_at/last_used_at/expires_at/revoked_at`（**永不返回明文**） |
| `DELETE /auth/api-keys/{id}` | 软吊销（`revoked_at=now`），**下一请求即失效**（无缓存窗口）；仅 Key 归属人可操作 |

安全配套：发放端点挂自研限流（5 次/分/IP，沿用 `AUTH_RATE_LIMIT_*` 配置惯例，新键 `AUTH_RATE_LIMIT_API_KEY_CREATE`）；`api_key.create` / `api_key.revoke` 接入现有 `record_audit`；吊销用户（未来删号）时级联吊销其 Key。

#### E.6.6 前端 UI 设计（SettingsView「API 密钥」子页面）

**信息架构**：独立子路由页（非塞入常规设置——资源管理与偏好配置性质不同）。沿用现有模式：`router/index.ts` 注册 `/settings` children 子路由 `SettingsApiKeys` → `views/Settings/ApiKeysView.vue`；`SettingsView.vue` 菜单追加 `{ name: 'api-keys', path: '/settings/api-keys', label: 'API 密钥', icon: KeyRound }`（lucide）。数据层新增 `queries/apiKeys.ts`（useQuery 列表 + useMutation 创建/吊销），**响应类型定义为精确 `UserApiKey` interface**（与专项 C1 类型化方向一致，不走 `Record<string, unknown>`）。

**页面布局**（风格对齐 GeneralView：`max-w-2xl` 居中、Tailwind 自绘卡片 + Element Plus 控件、全量 `dark:` 跟随）：

```
┌─────────────────────────────────────────────────────┐
│ API 密钥                                    [+ 新建密钥]  │  ← h1 + primary 按钮（达上限 disabled+tooltip）
│ 以你的身份访问 API（MCP 客户端、脚本、CI 集成）。             │
│ ┌───────────────────────────────────────────────┐ │
│ │ ⓘ 密钥明文仅在创建时显示一次；如泄露请立即吊销。       │ │  ← info 说明条（蓝底 border-l-4）
│ └───────────────────────────────────────────────┘ │
│ ┌─ 密钥列表（白卡 rounded-xl shadow-sm）─────────────┐ │
│ │ 名称          密钥            状态   最近使用   操作  │ │
│ │ claude-code   rag_a1b2c3d4…  ●活跃  2 分钟前   [吊销] │ │  ← 密钥列等宽字体 font-mono
│ │ ci-script     rag_9f8e7d6c…  ●已过期  30 天前    —   │ │
│ │ old-laptop    rag_5a4b3c2d…  ○已吊销  —        —   │ │  ← 已吊销/过期行整行置灰 opacity-60
│ └───────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

- 列表实现：`el-table`（对齐 DocumentTable 惯例），列 = 名称 / 密钥前缀（`font-mono` + `…` 截断）/ 状态 badge（`el-tag`：活跃=success、已过期=warning、已吊销=info）/ 创建时间 / 最近使用（相对时间，`useTimeAgo`，VueUse 已有依赖）/ 操作。
- 操作列：仅活跃行显示「吊销」（danger plain 小按钮）；已吊销/已过期行无操作。
- 空列表：居中空态（KeyRound 大图标 + "还没有 API 密钥" + 新建引导按钮）。
- **功能未启用空态**（`USER_API_KEYS_ENABLED=false`）：整个列表区域替换为说明卡（"该功能未启用，请检查后端配置 USER_API_KEYS_ENABLED"），新建按钮隐藏。开关值来自 `GET /config/info` 新增字段 `user_api_keys_enabled`（后端配套，见 E.6.7 补充）。

**创建流程：两步对话框**（`el-dialog`，宽 520px，`append-to-body`）：

- **Step 1 表单**：
  - 名称（必填）：`el-input`，maxlength=64，placeholder「用途备注，如 claude-code-office」——审计追溯依赖此项；
  - 有效期（可选）：`el-select`（永久 / 30 天 / 90 天 / 180 天），默认永久，前端计算 `expires_at`（UTC）；
  - footer：「取消」+「创建」（loading 态防重复提交）。
- **Step 2 成功态**（同一 dialog 内切换内容，**不关闭**）：
  - 顶部 success 提示「密钥已创建」；
  - 完整明文：等宽字体展示于深色底代码块（`bg-gray-900 text-gray-100 rounded-lg p-4 font-mono break-all`）+ 「复制」按钮（VueUse `useClipboard`，成功 toast「已复制到剪贴板」）；
  - warning 说明条：「请立即保存到密码管理器，关闭后无法再次查看」；
  - footer：「我已保存，关闭」（关闭后 invalidate `['api-keys']` 刷新列表）。
- **防丢保护（关键细节）**：Step 2 状态下点击 ×/ESC/遮罩均不直接关闭——先弹 `ElMessageBox.confirm`「密钥尚未保存，关闭后无法再次查看。确认已保存？」，确认才关闭；对话框禁用 `close-on-click-modal`。

**吊销流程**：行内「吊销」→ `ElMessageBox.confirm`（`type: 'warning'`，正文带 key_prefix：`确认吊销 rag_a1b2c3d4…（claude-code）？使用该密钥的客户端将立即失去访问权限。`）→ mutation 成功 toast「已吊销，下请求即失效」+ 刷新列表。**不需要输入名称二次确认**（吊销可逆性虽无，但风险等级低于删库类操作，且列表中有 prefix 可核对——与 HistoryView 清空历史同档位）。

**边界与错误**：加载骨架（`el-skeleton` 3 行）；全部 mutation 错误走 `handleMutationError`（W6 #35 惯例）；401 由 axios 拦截器统一处理；创建返回 422（超上限）时定位到名称字段下方展示后端 detail。

**组件拆分**：`ApiKeysView.vue`（页面 + 列表卡）+ `CreateApiKeyDialog.vue`（两步对话框，`v-model:visible` + emit created）；单测：`apiKeys.spec.ts`（列表渲染三态 badge / 吊销 confirm 流 / 上限禁用 / Step2 防丢确认，约 5 用例，补入 D4 覆盖率基数）。

#### E.6.7 配置与灰度

- `USER_API_KEYS_ENABLED`（默认 **false**，灰度开启，沿用 bool 开关惯例）；`USER_API_KEYS_MAX_PER_USER=5`；`AUTH_RATE_LIMIT_API_KEY_CREATE=5`——三者同步 `.env.example` 与 env 一致性检查。
- **后端配套**：`GET /config/info` 响应新增 `user_api_keys_enabled` 字段（前端按此渲染"功能未启用"空态，见 E.6.6）；开关关闭时 `/auth/api-keys` 三端点直接 404（不暴露面）。
- 开关关闭时认证链跳过第 2 步（零开销），现有全局 API_KEY / JWT 行为完全不变。

#### E.6.8 验收标准

- 单测：发放 → 用 Key 调 `/auth/me` 返回绑定用户；吊销后 401；过期 Key 拒绝；超出上限 422；他人 Key 无法吊销他人资源（403）；**全局 API_KEY 与 JWT 路径回归不变**（`api_key_user` 既有测试保持绿）。
- WS 首帧 `api_key` 走 per-user Key 同样返回绑定用户。
- 审计出现 `api_key.create` / `api_key.revoke` 两条记录；前端 typecheck/eslint/54 单测全绿。
- gitleaks 扫描无新增误报（Key 明文只出现在 API 响应与用户剪贴板，不入库）。

#### E.6.9 实施规模与排期

后端 1 表（create_all 自动建表，同 audit_log 先例）+ auth 链插入 + 3 端点 + 配置 3 键；前端 1 卡片；测试约 8-10 用例。单批可完成。**排期：不占用 D/C 时段，作为阶段一之后、阶段二之前的独立小批**；因修复 `api_key_user` 归属串号隐患，也可按需提前。

---

## 执行约定

- 全部专项遵循「设计先行 → 确认后实施」；每个专项启动时输出独立实施记录（沿用 fix-plan 的 ✅ 惯例回写本文件）。
- 回归基线：后端 976 passed / 102 skipped；前端 vue-tsc 零错误、eslint 0 error、54 单测 + 5 E2E；CI 五 job 全绿。
- A/B 两个渐进式专项以触发器挂起状态跟踪，每季度例行复核一次触发条件是否达成（或随评审一并复核）。
- 新增依赖硬约束：开源可离线、禁止付费 API（Zod 决策、虚拟滚动库选型均已按此约束裁剪）。
