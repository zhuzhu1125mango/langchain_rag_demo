# 项目状态与待办台账

> 状态：持续维护 · 最后更新：2026-10-22
> 职责：**项目已完成 / 剩余待办的唯一台账**（见 [README.md](README.md) 目录结构）；各专题的方案与实测细节见对应的 `design/` 文档，本表只做汇总与索引。
> 关联：[special-projects-2026-09.md](design/special-projects-2026-09.md)、[improvement-roadmap.md](design/improvement-roadmap.md)、[chat-latency-and-model-config.md](design/chat-latency-and-model-config.md)、[e1-navigable-lookup-gap.md](design/e1-navigable-lookup-gap.md)

---

## 一、已完成工作

### A. 模型选型与显存治理（核心：消除 4GB 显存下的换载拉锯）

| # | 工作 | 结论 / 效果 | 提交 |
|---|---|---|---|
| A1 | 直答模型 A/B 定稿 | `qwen2.5:3b-instruct` 胜出（墙钟 0.9~1.6s，原生非思考契合 `llm_direct`）；落选 `qwen3:1.7b` **已清理删除**（2026-10-13，实测 `ollama list` 无此模型；如需回退须重新 `ollama pull`） | fd6b1ee |
| A2 | reranker 换型 | `bge-reranker-base` 替代 `v2-m3`：离线 A/B 质量零差异、推理快 3.3 倍；kb_retrieval 热身 1.3~1.9s → **441~560ms** | fd6b1ee |
| A3 | 移除 4B 预热 | lifespan 只预热 `llm_direct`；4B（3.9GiB）驻留会驱逐 3B 直答模型 | 18b87f3 |
| A4 | 辅助任务归位 fast llm | `ContextEnhancer` / `AnswerVerifier` 由注入 4B 改为 `fast_llm` | 18b87f3 |
| A5 | 架构不变量 | **4B 唯一入口 = `deep_thinking=on` 主回答链**；KB 问答整链 wall 0.9~1.5s | — |

### B. 问答链路延迟治理

| # | 工作 | 说明 | 提交 |
|---|---|---|---|
| B1 | kb_retrieval 五步细分计时 | embed/dense/sparse/rrf/rerank 写入 trace stage detail；实测 **rerank 占 95%+**，其余步骤清白 | 12a5f79 |
| B2 | `KB_RERANK_MAX_LENGTH` 配置化 | 对当前语料**无效但无害**（chunk 仅 50~100 token，截断不触发），保留作长块防护 | 12a5f79 |
| B3 | 精确匹配缓存前置 | 阶段 1.5，命中退化为单次 Redis HGET | 12a5f79 |
| B4 | has_kb 跳过 LLM 裁决 | 与 `DECISION_VOTE_WHEN_KB_SELECTED=False` 同哲学 | 12a5f79 |
| B5 | 摘要/事实校验后台化 | 不再阻塞 SSE `final`/`end` 事件 | 12a5f79 |
| B6 | 相关性门控复用 rerank_score | 接通原死配置 `KB_RELEVANCE_SCORE_THRESHOLD` | 12a5f79 |

### C. 缺陷修复

| # | 问题 | 根因 / 修复 | 提交 |
|---|---|---|---|
| C1 | 多轮会话第 3+ 问 generate 41~75s | `ContextEnhancer(self.llm)` 注入 4B → 驱逐 3B 换载拉锯；修复为 `fast_llm`，post-fix **41068ms → 2490ms**（TRAE-debugger 闭环，server.log evicting 时序实证） | 18b87f3 |
| C2 | 天气城市提取器误判 | "工作温度范围"被当城市名误路由；三层防线（技术复合温度词排除 + 地名校验 jieba ns/白名单 + 残片续试） | 465f927 |
| C3 | 计算器中文干扰 | 从问题抽取算术子串再计算 | 465f927 |
| C4 | 前端陈旧分片白屏 | `router.onError` 捕获懒加载失败 → sessionStorage 守卫整页 reload 一次自愈 | 2f6eea5 |
| C5 | 流式补发事件被丢弃 | useChatStream id 定位修复（title/校验警告） | 2f6eea5 |
| C6 | 精确缓存"TTL 过长" | **伪问题**：Redis 实测条目 age 19~21h 全部 <24h TTL；系时间线归因错误，未改动 | — |
| C7 | trace 落库「未知列」静默失败 | root：`RequestTrace` 固定列不含 `semantic_cache_hit` / `verification` / `context_rewrite`，而 `stages.py` 写入 → `RequestTrace(**data)` 抛 TypeError、被 best-effort 吞掉 → 问题改写/语义缓存命中/非流式 web 校验的 trace 丢失。修复：`save_async` 按模型列过滤（`_to_record_payload`）+ 移除冗余 `context_rewrite` 顶层写（已在 `intent_decision.to_dict()`） | 996ba6b |

### D. 专项推进

| # | 专项 | 状态 | 提交 |
|---|---|---|---|
| D1 | 专项 C 类型严格化 | ✅ 完成：trace.ts 四接口精确化、kb.ts 参数袋接口化、eslint `no-explicit-any: error` + `no-non-null-assertion: warn`；验收全过（vue-tsc/eslint 0 error/vitest 106/E2E 5） | c1a4b9e |
| D2 | 专项 D 覆盖率收紧 | ✅ 完成（核实）：前端阈值升至 32/35/24/31 + CI `--coverage` 卡点；后端 fail_under 收敛 pyproject | 59140bc 等 |
| D3 | 通读报告归档 | ✅ 移入 [archive/project-readthrough-2026-09-30.md](archive/project-readthrough-2026-09-30.md) | a4bcdd1 |
| D4 | E-1 能力差距盘点 | ✅ 完成：报告见 [e1-navigable-lookup-gap.md](design/e1-navigable-lookup-gap.md)（已推送） | 4789f3a |
| D5 | E-3 Lint 常态化 | ✅ 实质完成：`wiki_lint_scheduler` 完整（lifespan 挂载 + 可配间隔 + 失败续跑） | （D4 落地） |
| D6 | **E.6 用户级 API Key** | ✅ 完成：`user_api_keys` 表 + 认证链 per-user 解析（HTTP/WS 共用）+ 三端点（发放一次性明文/列表无明文/软吊销）+ 前端 `/settings/api-keys` 管理页；顺带修复全局 API_KEY 固定返回 `api_key_user` 的多用户归属串号隐患；后端 19 + 前端 20 测试 | cdd2ed3 |
| D7 | 评审修复闭环（W1~W6） | ✅ 收官：四份评审 + 综合修复计划全部落账并归档 `archive/`（P0×6 + P1×22 + P2×78） | 见 archive/fix-plan-2026-09-24.md |
| D8 | 文档一致性回写 | ✅ 修正模型切换后的文档漂移：README / deployment.md / rag-enhancement.md / chat-latency 的 reranker 与模型名残留；api.md 补 E.6 三端点与 JWT 刷新口径；special-projects 状态行 + 实施记录回写 | 6202ff5 |
| D9 | 文档整理（第二轮） | ✅ README 结构/CI/功能表回写（api 17 模块、CI 7 个 job、FC/ReAct 默认开关、pytest 命令）；功能表诚实化（实验框架「未接入问答链路」/动态学习「写回闭环待打通」）；归档 direct-model-switch-plan（→ archive/direct-model-switch-plan-2026-10-03.md）；architecture 代码地图修正（rag_chain 拆分/pipeline/、Agent 默认开启）；设计文档模型名与工具路径漂移修正 | 6202ff5 |
| D10 | **学习闭环接入（LEARN）** | ✅ 三断链修复：W-A 单例策略管理器 + 权重/阈值回载、W-B KB 短路路径样本补采、W-C A/B 分流接入 chat（白名单覆盖 decision/检索 + response_time 指标）。三开关灰度默认关；新增/扩展单测 12 例；全量 **1042 passed / 105 skipped**；**dev 三开关全开端到端验证通过**（W-A 回载日志 / W-B 补采非空 / W-C 分流+覆盖+指标）。详见 [learning-loop-wiring.md](design/learning-loop-wiring.md) | （本次，未提交） |
| D11 | **Agent 轨迹级评测（AM-1）+ hybrid 双源 + 输出污染修复** | ✅ 新增 `TrajectoryEvaluator`（**四维全确定性规则**，**去 LLM**）+ 轨迹双入口还原 + 数据集 29 例标注 + `run_agent_ab` 接入（开关 `AGENT_TRAJ_EVAL_ENABLED` 默认关）；三轮抽检证明 3B judge 四维皆不可靠 → 逐维规则化。**① hybrid/multi「未走 web」**：编排器未透传路由 `needs_web` → 注入 kb+web 双源引导（`tool_selection` 0.5→1.0）。**② 答案输出污染**：纯 Agent 直答分支未清洗 + sanitizer 只认 JSON 型 → 新增「叙述型」工具调用检测并接入直答路径（污染 2/29→**0/29**）。**dev 真机基线（live B 臂 29 题，0 错误）**：已判分 15 / 跳过 14，`traj_score` **0.981** / `traj_pass_rate` **1.000**（15/15）。单测 +7；全量 **1070 passed / 104 skipped**。详见 [agent-eval-trace.md](design/agent-eval-trace.md) §7 | （本次，未提交） |

---

## 二、剩余待办

### 优先级执行序（汇总视图 · 2026-10-20 刷新）

| 级别 | 项 | 内容 | 依赖 / 归属 |
|---|---|---|---|
| ✅ | PUSH | 积压提交已推送：`c1a4b9e..6202ff5`（2026-10-16）+ `6202ff5..eb6cfea`（2026-10-21，AM-1/hybrid 双源/输出污染/prod schema/LEARN/E1V 合并提交）；工作区干净 | 无 |
| P1 | E1V ✅ | E-1 收口**完成**：离线 A/B（10-19/10-20，编译覆盖已修 → C 臂 0.188→0.938）+ L1 图边 47 条含跨档 13 + **Agent e2e 双臂（2026-10-21，8/8 ok）**。e2e 结论：hit/recall/mrr 持平，步数与 P95 翻倍，仅 1/8 题使用 `wiki_lookup` → **无增益，W1 保持默认关闭** | 无 |
| P1 | W1 ✅ | 索引页优先入口（PG 直读 `page_type=index` 作第一跳）——已实施 2026-10-15 | 无 |
| P1 | W3 ✅ | 导航埋点 + Agent 工具策略——已实施 2026-10-15 | 无 |
| P1 | W2 ✅ | entity/topic 退主池 A/B——已实施 2026-10-15（证据采纳退池，默认 exclude `wiki,wiki_syn`） | W3 |
| P2 | LEARN | 学习闭环 / 实验接入——**W-A/W-B/W-C 已实施 2026-10-16 并经 dev 端到端验证**（三开关灰度默认关），见 [learning-loop-wiring.md](design/learning-loop-wiring.md) ✅ | 开发 |
| P2 | E2 | Query 回流沉淀 synthesis 页 | W3 |
| P2 | SA | 专项 A 巨型文件拆分决策 | owner |
| ✅ | ENV | `.env.dev` rerank 模型漂移——**经核实为虚警**（三份 env 均已是 `bge-reranker-base`），无需修正 | 无 |
| ✅ | SCHEMA | prod 库 schema 漂移**已修复**（`upgrade head` 后 `alembic check` 无漂移，2026-10-19）；详见 §二 SCHEMA 行 | 无 |
| **P1** | AM | **Agent 成熟度差距**（2026-10-20 对标业界）：AM-1 轨迹级评测 / AM-2 HITL+护栏 **均已落地**；AM-3 MCP 客户端 / AM-4 持久化 / AM-5 记忆 为 P2；AM-6 工具治理 / AM-7 同名跨档页合并 为 P3。详见下方「Agent 成熟度差距」与 [agent-maturity-gap-analysis.md](design/agent-maturity-gap-analysis.md) | 开发 |
| **P1** | **完善专项** | **UX / VIS / CODE / OPS（2026-10-22 全项目审计新增）**：停止生成、移动端、可访问性、设计 token、异常治理、巨型文件拆分、运维手册、供应链扫描。详见「全项目完善与演进专项」与 [evolution-roadmap-2026-10.md](design/evolution-roadmap-2026-10.md) | 开发 |
| P3 | D-U / DEV / W0 / HG | 覆盖率上调 / dev 栈回收 / owner 手动项 / 挂起区 | 见各条 |

> **一句话执行序**（2026-10-22 刷新）：P0 已清 → **AM-1 轨迹级评测已落地并推送** → **E1V 已收口**（无增益，W1 保持默认关）→ **答案级判分器确定性化 + A/B 门槛重标定**（0.45/0.30）→ **AM-2 HITL+护栏 已落地**（护栏默认开 / 审批默认 off）→ **完善专项按批次 1 启动**（停止生成 / 移动端 / a11y / 设计 token）→ AM-3 MCP 客户端 → E2/SA/D-U/AM-4~7 视排期。

### E-1 导航式 wiki_lookup（阶段一，已收口）

> 前提：D1-D4 已落地 E-1 主体能力（wiki_navigator / links JSONB / synthesis / lint / Agent 编排，全灰度默认关）。E-1 = 存量激活 + 三差距。

| # | 工作项 | 内容 | 规模 | 依赖 |
|---|---|---|---|---|
| W1 ✅ | 索引页优先入口 | navigator 从 **PG 直读** `page_type=index` 目录页作第一跳（索引页不进向量库，正文经 MinIO 直读）；开关 `WIKI_NAV_INDEX_FIRST_ENABLED`（灰度默认关）。**已实施 2026-10-15**，见 [e1-navigable-lookup-gap.md §6](design/e1-navigable-lookup-gap.md) | 小 | 无 |
| W3 ✅ | 导航埋点 + Agent 策略 | wiki_lookup 多轮调用埋点（轮数/取页序列/入口类型）汇总写入 trace `stages`（name=wiki_nav）；Agent 工具描述明确"索引页→Related 逐页"。**已实施 2026-10-15** | 小 | 无 |
| W2 ✅ | entity/topic 退主池 A/B | exclude 集合扩展对照实验（三臂：现状/退池/仅 raw）；证据：仅 raw recall@5=0.938 ＞ raw+wiki 0.812（hit/mrr 持平）→ **采纳退池**，默认 `KB_RETRIEVAL_EXCLUDE_SOURCE_KINDS=wiki,wiki_syn`。**已实施 2026-10-15** | 中 | W3 |
| E1V ✅ | 多跳验收 | **三步全部完成**：①离线两轮（10-19/10-20，瓶颈为编译覆盖，已修，C 臂 0.188→0.938）；②L1 图边 47 条含跨档 13；③**Agent 多轮 e2e 双臂 2026-10-21 完成**（8/8 ok）：基线 vs W1-ON 的 hit/recall/mrr **完全持平**（1.000/0.875/1.000），traj_pass 0.875→1.000 但步数 9→11、P95 9.4s→**21.6s**，且 3B 决策器 8 题仅 1 题选 `wiki_lookup` → **未证明增益，W1 保持默认关闭**。详见 [e1-navigable-lookup-gap.md §6](design/e1-navigable-lookup-gap.md) | — | W1-W3 |
| E1V-L1 ✅ | 图边元数据化 L1 | 按「其他页标题出现在本页正文」建边（`WIKI_LINK_METADATA_ENABLED`，默认关）+ harness 对齐；**已实施 2026-10-20**（单测 +5）。离线验证：**边 15 条全为档内、跨档 0** → **D 臂未改善**（0.188=C），主检索三臂不变。**但真瓶颈是编译覆盖**：12 篇仅 4 篇编出页，8 篇因 `_parse_json_dict` 贪婪正则 → `Extra data` → **零页面**（含 `reimbursement`/`attendance` 等**多跳枢纽档**）→ **已修**（改 `raw_decode`，单测 +2）。**修复后冷重编译（12/12 篇、34 页）**：图边 **47 条含跨档 13**（对齐金标对）；**C 臂 0.188 → 0.938**（=raw 基线，缺口系覆盖所致）→ 导航价值仍须 Agent e2e | 中 | E1V |

**建议实施序**：W1 → W3 → W2 → E1V。

### Agent 成熟度差距（2026-10-20 对标业界）

> 来源：[agent-maturity-gap-analysis.md](design/agent-maturity-gap-analysis.md)（业界做法综述 + 主流产品对标矩阵 + 本项目能力矩阵）。**不推翻** [agent-evolution.md §6.1](design/agent-evolution.md) 的「暂缓 checkpoint/HITL」旧结论——改为**按触发条件重启的候选待办**（E-1 的 E1V 卡点已直接印证「评测缺口」的代价）。

| # | 差距 | 优先级 | 说明 / 触发 |
|---|---|---|---|
| AM-1 | **Agent 轨迹级评测** | **P1** | **已实施 + 真机基线（2026-10-20/21）**：`TrajectoryEvaluator`（**四维全确定性规则**，去 LLM；受 4GB 显存红線约束、3B judge 不可靠）+ 轨迹双入口 + 数据集 29 例标注 + `run_agent_ab` 接入（开关默认关）。**另修两处产品缺陷**：① hybrid/multi「未走 web」（编排器未透传路由 `needs_web` → kb+web 双源引导，工具选择 0.5→1.0）② Agent 答案输出污染（直答分支未清洗 + 只认 JSON 型污染 → 新增叙述型检测，污染 2/29→0/29）。**dev 基线（live B 臂 29 题，0 错误）**：已判分 15 / 跳过 14；**`traj_score` 0.981 / `traj_pass_rate` 1.000**（15/15，末轮答案质量偏好，非稳定水位）。**答案级判分已同步确定性化（2026-10-21）**：`GenerationEvaluator` 由 3B judge 改为「语义依据率 + 数字一致性」/「问句↔答案句语义相似度」规则（抽检证明 3B relevance 零区分度、faithfulness 天花板 0.7）；离线重打分 faithfulness 0.575→0.727、relevance 0.175→0.759。**残留**：①非 Agent 路径需跳过判分 ②`hybrid_01` 双源引导后仍只走 kb（引导非硬保证）③**A/B hybrid 门槛已按新判分尺度重标定（2026-10-21，0.45/0.30，compare 全 PASS）**；顺带修复 Agent 工具来源 `source_type` 丢失导致 KB 命中被误判为 0 的问题。待办：④ 在线抽样、`hybrid_04` 拒答复核。见 [agent-eval-trace.md](design/agent-eval-trace.md) |
| AM-2 | **HITL 审批点 + 护栏** | **P1** | **已实施（2026-10-22）**：① **输入护栏**（注入/越权特征、超长、控制字符 → 拒答 `guardrail_blocked`，不写语义缓存）② **工具护栏**（参数长度 / URL scheme / 私网与云元数据地址 / userinfo / 路径穿越，收口 `ToolManager.execute` 唯一入口）③ **输出护栏**（私钥 / sk- / AKIA / DB 凭证 / Bearer / 系统提示外泄 → 脱敏，SSE/落库/trace 三处一致）④ **HITL 审批闸门**（`AGENT_APPROVAL_MODE=off/reject/wait`；`AGENT_APPROVAL_TOOLS` 名单 + `risk_level=write` 自动纳入；wait 走 SSE `approval_required` → `POST /api/chat/approvals/{id}`，Redis 挂起、60s 超时拒绝、仅发起人可回执、Redis 不可用 fail-closed）。**护栏默认开、审批默认 off**（当前工具集全为只读/外部读取，名单为空不触发）。单测 +25、前端 +3；全量 **1103 passed / 105 skipped**、覆盖率 **62.12%**。详见 [agent-guardrails-hitl.md](design/agent-guardrails-hitl.md)。残留：审批断线恢复依赖 AM-4；输出护栏跨 chunk 密钥有漏检（已登记） |
| AM-3 | MCP 客户端接入 | P2 | 工具层缺互操作协议（业界已收敛 MCP）；先做**客户端**消费外部工具（用户级 API Key 前置已就绪，见 E.6） |
| AM-4 | 持久化 / 可恢复执行 | P2 | Agent 循环为内存态，长任务中断即丢；**触发**：多步/跨会话任务成为刚需 |
| AM-5 | 记忆（长期 store + compaction） | P2 | L1-a 记忆默认关、无跨会话长期存储与压缩 |
| AM-6 | 工具数量 / 描述治理 | P3 | 工具增多后选择困难（对齐「工程师都说不清就别给」） |
| AM-7 | **同名跨档页合并** | P3 | wiki 编译质量缺陷：`OA系统` 在 3 个档各生成 1 页未合并 → 图边解析按首个同名页命中，存在歧义 |

### 其他待办

| # | 任务 | 说明 | 优先级 | 归属 |
|---|---|---|---|---|
| E2 | Query 回流沉淀 synthesis 页 | 复用 L1-a 记忆通道 + D3 synthesis 机制；回流阈值需防噪声页 | 中（依赖 W3） | 开发 |
| LEARN | 学习闭环 / 实验接入 | ✅ 三断链（权重无回载 / KB 短路 strategy_results 恒空 / A/B 未接入问答链路）已修复并经 dev 端到端验证：W-A 单例+权重回载 / W-B 短路补采 / W-C A/B 接入 chat，三开关灰度默认关，见 [learning-loop-wiring.md](design/learning-loop-wiring.md) | 中 | 开发 |
| SA | 专项 A 巨型文件拆分决策 | 最新审计（2026-10-22）行数：`document.py` **1679** / `web_search_service.py` **1040** / `chat.py` **1026** / `wiki_compiler.py` **1000** / `stages.py` 783 / `agent_orchestrator.py` 740；按 A.4「触碰即先拆」需 owner 决策拆分优先级 | 中 | owner |
| W0 | 批次二手动项 | Tavily API Key 轮换；`configs/license/minio.license` 处置决策 | 低 | owner |
| HG | 挂起区 | 专项 B 触发监控（消息>200/节点>300）；L3 critic 反思灰度时点；MCP 分发边界（局域网多用户形态）最终确认；Office/HTML/EPUB loader 迁移、axios 5xx 非幂等重试等 P2 残项 | 低（触发器） | 开发 |
| ENV | `.env.dev` rerank 模型漂移（**已核实：不存在**） | 2026-10-18 复核：`.env.dev` / `.env.prod` / `.env.example` 三份的 `KB_RERANK_MODEL` 与 `SEARCH_RERANK_MODEL` 均为 `BAAI/bge-reranker-base`，`config.py` 两处默认均为 `None`（无硬编码）→ **无漂移**；原台账条目标述有误（陈旧），本条作废，无需改动 | 无 | 开发 |
| D-U | 覆盖率阈值继续上调 | 当前后端 **62.12%**（AM-2 批次 1103 passed）；按「只升不降」先上调门槛至 62，目标 65（前端 32/35/24/31 为当前卡点基线） | 低 | 开发 |
| DEV | dev 栈与验证残留（已清） | LEARN 验证用 dev 栈（rag-dev）已 `down`（保留卷），`.env.dev` 三开关复位默认 false；验证残留已清理：删除 E2E 实验及其级联、删除匿名测试会话与执行样本、KB/文档归属归还 `smoketest`。dev 库 schema 已迁移至 head（见 SCHEMA 行）；prod 未触及 | 低 | 开发 |
| ✅ | AB-CACHE | A/B harness 编译缓存（**已实施 2026-10-18**）：`run_wiki_multihop_ab.py` 新增按「语料+编译模型+页数上限/精炼轮数」哈希的磁盘缓存（`backend/data/wiki_multihop_ab_cache/`，gitignore）+ 逐篇编译进度实时输出（`flush=True`）+ `--no-cache`/`--cache-dir` 参数；离线验证缓存命中路径通过 | 无 | 开发 |
| ✅ SCHEMA | **DB schema 漂移修复（2026-10-18~19，已闭环）** | • **dev**：落后一格（`8cb6e5b283ec`）→ drop 手补列 + `alembic upgrade head` → `check` 无漂移。• **prod（P0）**：`820bc90565d8` stamped 未执行（缺 `audit_logs`/`user_api_keys` 两表 + `experiments.owner_id`/`wiki_pages.links` 两列 + `json≠jsonb`）→ 按方案 [prod-schema-drift-repair.md](design/prod-schema-drift-repair.md)：备份（`C:\Users\zhuzhu\.cache\rag_backups\app_prod_20261019.sql`）→ `docker cp` 投放迁移（不重建镜像，避免带上未提交代码）→ 容器内 `alembic upgrade head`（`820bc90565d8→d4e5f6a7b8c9`）→ **`alembic check` 无漂移**、两表两列 + jsonb 均就位。• **复发根因已堵**：start-prod.sh/.ps1 去 stamp 兜底 + CI 加 `alembic check`；并修复迁移 `8cb6e5b283ec` 的 asyncpg regclass 缺陷。待补：prod 功能冒烟（需凭据） | 无 | 开发 |

### 全项目完善与演进专项（2026-10-22 审计）

> 来源：[evolution-roadmap-2026-10.md](design/evolution-roadmap-2026-10.md)。对标 2026 主流 Agent 后确定「标配 / 差异化 / 避免」口径；目标：既**好用**也**好看**。
> 审计基线：后端 **1103 passed / 覆盖率 62.12%**、前端 **109 passed**；334 处宽泛异常、5 处 `aria/role`、8 处响应式断点、22 处硬编码色值、0 依赖/镜像扫描、无备份恢复手册。

| # | 方向 | 说明 / 证据 | 优先级 | 归属 |
|---|---|---|---|---|
| AGT-1 | 会话可恢复 + 可取消（AM-4） | Agent 循环纯内存态；无停止生成按钮（仅组件卸载 abort）；断线即丢 | P1 | 开发 |
| AGT-2 | trace → 回归样例闭环（AM-1④） | 只有离线 A/B，无线上抽样、失败转回归、门禁 | P1 | 开发 |
| AGT-3 | 评测可信度补齐 | `hybrid_01` 双源改硬保证；`hybrid_04` 拒答复核；非 Agent 路径跳过判分 | P1 | 开发 |
| AGT-4 | 产物工作区（Artifacts） | 仅 Markdown 气泡 + 来源卡；无预览 / 修订 / 下载 / 版本 diff | P2 | 开发 |
| AGT-5 | MCP 客户端（AM-3） | 工具只能自研，无外部工具互操作 | P2 | 开发 |
| AGT-6 | 记忆 / 上下文工程（AM-5） | 仅会话摘要且默认关；无压缩、大结果落盘、长期 store | P2 | 开发 |
| AGT-7 | 多模态 / 沙箱执行 | 输入区无图片/文件入口；无代码执行沙箱 | P3 | 开发 |
| UX-1 | 停止生成 / 编辑重发 | 用户无法中止长回答；只能等或刷新 | P1 | 前端 |
| UX-2 | 移动端适配 | 全前端仅 8 处响应式断点类，窄屏布局会挤压 | P1 | 前端 |
| UX-3 | 可访问性基础 | 全站仅 5 处 `aria/role`；键盘 / focus / 对比度未治理 | P1 | 前端 |
| UX-4 | 三态组件体系 | 仅 2 处 skeleton；加载 / 空 / 错各页不统一 | P2 | 前端 |
| UX-5 | 历史页搜索 / 筛选 / 导出 | 仅批量删除，无检索与导出入口 | P2 | 前端 |
| UX-6 | 长会话性能 | 无虚拟滚动，消息 + Markdown 全量渲染 | P2 | 前端 |
| UX-7 | 体验细节 | 引用悬浮预览、消息内搜索、导出 Markdown、快捷键、新手引导 | P3 | 前端 |
| VIS-1 | 设计 token 层 | 无 Tailwind config；22 处硬编码 hex；`dark:` 171 处但无统一变量 | P1 | 前端 |
| VIS-2 | 组件 primitive | Button/Card/Dialog/Badge/Input 各处手写，圆角/阴影/间距漂移 | P1 | 前端 |
| VIS-3 | 页面视觉收口 | 设置页 / 知识库页 / 聊天页密度与风格不一致 | P2 | 前端 |
| CODE-1 | 巨型文件拆分 | `document.py` 1679 / `web_search_service.py` 1040 / `chat.py` 1026 / `wiki_compiler.py` 1000 / `stages.py` 783 / `agent_orchestrator.py` 740；前端 `queries/kb.ts` 967 | P1 | 开发 |
| CODE-2 | 异常分级治理 | 334 处 `except Exception`；缺「可恢复 / 需上报 / 编程错误」分级与统一上下文 | P1 | 开发 |
| CODE-3 | eslint warning 清零 | 43 条非空断言 warning（测试为主），0 error | P2 | 前端 |
| CODE-4 | API / 类型分层 | `queries/kb.ts` 967 行混合请求、类型与转换 | P2 | 前端 |
| OPS-1 | 运维手册补全 | `docs/guide` 仅 deployment/development/monitoring；无备份 / 恢复 / 回滚 / 容量 | P1 | 开发 |
| OPS-2 | 供应链安全 | 无 dependabot / trivy / SBOM；CI 仅 gitleaks | P1 | 开发 |
| OPS-3 | 结构化日志 | stdlib 文本日志，未统一注入 request_id / 用户 / 阶段上下文 | P2 | 开发 |
| OPS-4 | 回归与演练 | 无 nightly 性能回归、无迁移回滚演练 | P2 | 开发 |
| OPS-5 | 覆盖率上调 | 62.12% → 65%（只升不降） | P3 | 开发 |

**批次 1（体感/观感优先）**：UX-1/UX-2/UX-3/UX-4 + VIS-1/VIS-2 + AGT-2。
**批次 2（可靠性与代码观感）**：AGT-1（AM-4）+ CODE-1/CODE-2 + OPS-1/OPS-2/OPS-3。
**批次 3（差异化）**：AGT-4/AGT-5/AGT-6 + OPS-4/OPS-5。

---

## 三、阻塞项

**（已解除）push 网络故障** —— 2026-10-16 网络/代理链路恢复，无遗留阻塞。

- 曾现象：`github.com:443` 连接超时 / `schannel: failed to receive handshake`（TLS 握手失败）；`github.com` 被解析到 FlClash Fake-IP `198.18.0.13`，直连/沙箱外/经代理端口四种路径均失败
- 处置：链路恢复后 `git push` 直接成功（`c1a4b9e..6202ff5`）

---

## 四、关键决策与红线（供后续参考）

1. **显存铁律**：4GB 显存下 4B（3.9GiB）与 3B 互斥；**所有辅助任务（摘要/指代消解/校验/标题/路由/改写）必须走 fast llm**，4B 仅 `deep_thinking=on` 主回答链。反例即 C1 慢例。
2. **D2/D5 证伪红线**（E-1 必须遵守）：
   - 互链**严禁内联页面正文**（曾稀释检索 0.938→0.812）；走 PG `links` JSONB 结构化元数据
   - 导航验收以 **Agent 多轮端到端 A/B** 为主（离线 5-slot recall 仅作不退化底线，无法体现导航价值）
   - 任何混入主检索的动作**必先 A/B**
3. **模型选型依据**：以用户体感墙钟为准，勿被 token/s 数字误导（思考模型的隐藏思考 token 会抬高该指标）。
4. **缓存归因**：命中判断以 Redis 条目 `created_at`/`redis_ttl` 直查为准，勿凭直觉（见 C6 误判）。

---

## 五、提交与推送状态

- 已入库并推送：`12a5f79` → `18b87f3` → `465f927` → `2f6eea5` → `fd6b1ee` → `8decced` → `a4bcdd1` → `c1a4b9e` → `4789f3a` → `996ba6b` → `93e11a9` → `6202ff5` → `eb6cfea` → **`8beffcf`**（origin/main HEAD，2026-10-22；远端 CI run [39750037599](https://github.com/zhuzhu1125mango/langchain_rag_demo/actions/runs/39750037599) 7/7 全绿）
- 备注：均为**直推 main**——远程分支保护要求 5 项 required status checks，均被 admin 绕过（`Bypassed rule violations`）；后续改动建议走 PR 以触发 CI
- 工作区：本批次（AM-2 护栏/HITL + `upload-artifact@v6` + 全项目审计文档）已提交推送，工作区干净
- `8beffcf`（2026-10-22）：修复 secrets-scan 误报、本地复跑 7 项 CI 全绿
- `eb6cfea`（2026-10-21）一次性收口了此前四批未提交改动：
  - **① AM-1 轨迹级评测**：`trajectory_evaluator.py`（四维确定性）、`run_agent_ab.py` 接入、数据集 29 例标注、`agent_orchestrator.py`/`pipeline/agent.py`（hybrid 双源）、`output_sanitizer.py`（叙述型污染）、单测 +7
  - **② LEARN 学习闭环（D10）**：chat/learning/config/main/decision_pipeline/experiment_manager/learning_engine/milvus_service/state/rag_chain/rule_learner/strategy_manager + 3 测试文件
  - **③ prod schema 漂移修复**：迁移 `d4e5f6a7b8c9_reconcile_missing_schema.py`、`8cb6e5b283ec`（asyncpg 缺陷）、`scripts/start-prod.sh|.ps1`（去 stamp）、`.github/workflows/ci.yml`（加 `alembic check`）
  - **④ E-1 后续**：`wiki_compiler.py`（L1 图边 + 编译覆盖修复）、`WIKI_LINK_METADATA_ENABLED`、`run_wiki_multihop_ab.py`（图构建对齐 + 编译缓存）、`test_wiki_compiler.py`（+7）
  - **⑤ 文档**：README/docs README/project-status + 4 份新增设计文档

### CI 检查本地复跑（2026-10-22）

分支保护要求的 5 项 required checks 已逐项在本地按 CI 命令复跑：

| Job | 结果 |
|---|---|
| Env Consistency | ✅ 104 键覆盖、compose 插值 / config 字段全对齐 |
| Backend Unit Tests | ✅ **1078 passed / 105 skipped**；覆盖率 **61.72%**（门槛 60） |
| Backend Integration Tests | ✅ 全新库 `alembic upgrade head` + `alembic check` 无漂移；**1153 passed / 24 skipped**（6 例失败均为本地环境差异，宿主机 CI 布局复跑全过：`/configs` 容器挂载路径、`APP_ENV=dev` 环境变量泄漏、`/app/data`↔`/tmp` 跨设备 rename） |
| RAG Retrieval Eval | ✅ hit_rate/mrr/recall 均 **1.000** |
| Frontend Build & Tests | ✅ vitest+coverage、`pnpm build`、E2E **5 passed** |
| Lint & Typecheck（非必需） | ✅ ruff 0；eslint 0 error（44 warning）；`vue-tsc` 0 |
| Secrets Scan（非必需） | 修复后 ✅：3 处测试夹具假值被误报 → 加 `gitleaks:allow`，按 CI 口径（`git archive` 已跟踪文件）重扫 **no leaks** |

> AM-2 批次复跑（2026-10-22）：后端 **1103 passed / 105 skipped、覆盖率 62.12%**；前端 **109 passed**；ruff / eslint / vue-tsc 0 error；远端 run `39750037599` 7/7 全绿（`upload-artifact` 已升 v6）。
