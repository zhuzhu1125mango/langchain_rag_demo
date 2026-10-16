# 项目状态与待办台账

> 状态：持续维护 · 最后更新：2026-10-15
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
| C7 | trace 落库「未知列」静默失败 | root：`RequestTrace` 固定列不含 `semantic_cache_hit` / `verification` / `context_rewrite`，而 `stages.py` 写入 → `RequestTrace(**data)` 抛 TypeError、被 best-effort 吞掉 → 问题改写/语义缓存命中/非流式 web 校验的 trace 丢失。修复：`save_async` 按模型列过滤（`_to_record_payload`）+ 移除冗余 `context_rewrite` 顶层写（已在 `intent_decision.to_dict()`） | （本次，未提交） |

### D. 专项推进

| # | 专项 | 状态 | 提交 |
|---|---|---|---|
| D1 | 专项 C 类型严格化 | ✅ 完成：trace.ts 四接口精确化、kb.ts 参数袋接口化、eslint `no-explicit-any: error` + `no-non-null-assertion: warn`；验收全过（vue-tsc/eslint 0 error/vitest 106/E2E 5） | c1a4b9e |
| D2 | 专项 D 覆盖率收紧 | ✅ 完成（核实）：前端阈值升至 32/35/24/31 + CI `--coverage` 卡点；后端 fail_under 收敛 pyproject | 59140bc 等 |
| D3 | 通读报告归档 | ✅ 移入 [archive/project-readthrough-2026-09-30.md](archive/project-readthrough-2026-09-30.md) | a4bcdd1 |
| D4 | E-1 能力差距盘点 | ✅ 完成：报告见 [e1-navigable-lookup-gap.md](design/e1-navigable-lookup-gap.md)（**待 push**） | 4789f3a |
| D5 | E-3 Lint 常态化 | ✅ 实质完成：`wiki_lint_scheduler` 完整（lifespan 挂载 + 可配间隔 + 失败续跑） | （D4 落地） |
| D6 | **E.6 用户级 API Key** | ✅ 完成：`user_api_keys` 表 + 认证链 per-user 解析（HTTP/WS 共用）+ 三端点（发放一次性明文/列表无明文/软吊销）+ 前端 `/settings/api-keys` 管理页；顺带修复全局 API_KEY 固定返回 `api_key_user` 的多用户归属串号隐患；后端 19 + 前端 20 测试 | cdd2ed3 |
| D7 | 评审修复闭环（W1~W6） | ✅ 收官：四份评审 + 综合修复计划全部落账并归档 `archive/`（P0×6 + P1×22 + P2×78） | 见 archive/fix-plan-2026-09-24.md |
| D8 | 文档一致性回写 | ✅ 修正模型切换后的文档漂移：README / deployment.md / rag-enhancement.md / chat-latency 的 reranker 与模型名残留；api.md 补 E.6 三端点与 JWT 刷新口径；special-projects 状态行 + 实施记录回写 | （本次，未提交） |
| D9 | 文档整理（第二轮） | ✅ README 结构/CI/功能表回写（api 17 模块、CI 7 个 job、FC/ReAct 默认开关、pytest 命令）；功能表诚实化（实验框架「未接入问答链路」/动态学习「写回闭环待打通」）；归档 direct-model-switch-plan（→ archive/direct-model-switch-plan-2026-10-03.md）；architecture 代码地图修正（rag_chain 拆分/pipeline/、Agent 默认开启）；设计文档模型名与工具路径漂移修正 | （本次，未提交） |

---

## 二、剩余待办

### 优先级执行序（汇总视图 · 2026-10-15）

| 级别 | 项 | 内容 | 依赖 / 归属 |
|---|---|---|---|
| P0 | PUSH | 推送积压提交（`4789f3a` + 本次文档整理 D8/D9）；受 push 网络故障阻塞 | 解阻后 |
| P0 | E-1 | 进行中专项：W1 → W3 → W2 → E1V（纯本地可开工，不受 push 阻塞） | 见下 |
| P1 | W1 ✅ | 索引页优先入口（PG 直读 `page_type=index` 作第一跳）——已实施 2026-10-15 | 无 |
| P1 | W3 ✅ | 导航埋点 + Agent 工具策略——已实施 2026-10-15 | 无 |
| P1 | W2 ✅ | entity/topic 退主池 A/B——已实施 2026-10-15（证据采纳退池，默认 exclude `wiki,wiki_syn`） | W3 |
| P1 | E1V | 多跳端到端 A/B 验收 | W1–W3 |
| P2 | LEARN | 学习闭环 / 实验接入（经核实空转；README 已加限定说明） | 开发 |
| P2 | E2 | Query 回流沉淀 synthesis 页 | W3 |
| P2 | SA | 专项 A 巨型文件拆分决策 | owner |
| P3 | ENV | `.env.dev` rerank 模型漂移修正（一行配置） | 开发 |
| P3 | D-U / DEV / W0 / HG | 覆盖率上调 / dev 栈回收 / owner 手动项 / 挂起区 | 见各条 |

> **一句话执行序**：先解 push → 开 W1（本地可做）→ 并行 ENV 一行修正 → W1/W3 落地后接 W2/E2/E1V → LEARN/SA 视 owner 决策排期。

### E-1 导航式 wiki_lookup（阶段一，进行中）

> 前提：D1-D4 已落地 E-1 主体能力（wiki_navigator / links JSONB / synthesis / lint / Agent 编排，全灰度默认关）。E-1 = 存量激活 + 三差距。

| # | 工作项 | 内容 | 规模 | 依赖 |
|---|---|---|---|---|
| W1 ✅ | 索引页优先入口 | navigator 从 **PG 直读** `page_type=index` 目录页作第一跳（索引页不进向量库，正文经 MinIO 直读）；开关 `WIKI_NAV_INDEX_FIRST_ENABLED`（灰度默认关）。**已实施 2026-10-15**，见 [e1-navigable-lookup-gap.md §6](design/e1-navigable-lookup-gap.md) | 小 | 无 |
| W3 ✅ | 导航埋点 + Agent 策略 | wiki_lookup 多轮调用埋点（轮数/取页序列/入口类型）汇总写入 trace `stages`（name=wiki_nav）；Agent 工具描述明确"索引页→Related 逐页"。**已实施 2026-10-15** | 小 | 无 |
| W2 ✅ | entity/topic 退主池 A/B | exclude 集合扩展对照实验（三臂：现状/退池/仅 raw）；证据：仅 raw recall@5=0.938 ＞ raw+wiki 0.812（hit/mrr 持平）→ **采纳退池**，默认 `KB_RETRIEVAL_EXCLUDE_SOURCE_KINDS=wiki,wiki_syn`。**已实施 2026-10-15** | 中 | W3 |
| E1V | 多跳验收 | `wiki_multihop_eval_dataset` Agent 端到端 A/B（recall -0.062 缺口收复） | — | W1-W3 |

**建议实施序**：W1 → W3 → W2 → E1V。

### 其他待办

| # | 任务 | 说明 | 优先级 | 归属 |
|---|---|---|---|---|
| E2 | Query 回流沉淀 synthesis 页 | 复用 L1-a 记忆通道 + D3 synthesis 机制；回流阈值需防噪声页 | 中（依赖 W3） | 开发 |
| LEARN | 学习闭环 / 实验接入（经核实空转） | 学习权重无回载（`rag_chain._async_init` 与 `api/learning.py` 各自 `create_default_strategy_manager()` 新建实例；`sample_store.get_strategy_config` 无调用方）；A/B 实验分流（`experiment_manager.allocate_traffic`）仅被 `api/experiment.py` 自产自销，未接入问答链路 → README 功能表已加限定说明 | 中 | 开发 |
| SA | 专项 A 触发器决策 | `web_search_service.py`（893 行）已被 `12a5f79` 触碰 3 行，按 A.4「触碰即先拆」需决策是否补拆；`document.py`（1439 行）未触碰 | 中 | owner |
| W0 | 批次二手动项 | Tavily API Key 轮换；`configs/license/minio.license` 处置决策 | 低 | owner |
| HG | 挂起区 | 专项 B 触发监控（消息>200/节点>300）；L3 critic 反思灰度时点；MCP 分发边界（局域网多用户形态）最终确认；Office/HTML/EPUB loader 迁移、axios 5xx 非幂等重试等 P2 残项 | 低（触发器） | 开发 |
| ENV | `.env.dev` rerank 模型漂移 | `.env.dev` 的 `KB_RERANK_MODEL` / `SEARCH_RERANK_MODEL` 仍为 `bge-reranker-v2-m3`，而 `.env.prod` / `.env.example` / README 已统一为 `bge-reranker-base` → dev 重排更慢且与 prod 行为不一致，须同步（另 `KB_HYBRID_RERANK_TOP_K` prod/dev 均为 3，无漂移） | 低 | 开发 |
| D-U | 覆盖率阈值继续上调 | 前端 32/35/24/31、后端 60 为当前卡点基线；按「只升不降」约定随补测批次继续收紧（专项 D 已完成转入日常） | 低 | 开发 |
| DEV | dev 栈回收与冒烟残留 | `docker-compose.dev.yml` 拉起的 dev 栈仍在运行（backend/frontend/基础设施齐备）；冒烟测试注册的 `smoketest` 用户与 dev 会话留存 → 按需 `down`（保留卷）/ `down -v`（清卷） | 低 | 开发 |
| AB-CACHE | A/B harness 编译缓存 | `run_wiki_multihop_ab.py` 每次运行用 LLM 重编译全量语料（无缓存）→ 单次 1~2h 盲等（2026-10-15 W2 复跑 ~79min 未产出、中止）；加编译页缓存 + `python -u` 实时输出，E1V 复用 | 低 | 开发 |

---

## 三、阻塞项

**push 网络故障**：本地 commit `4789f3a`（E-1 盘点报告）未能推送。

- 现象：`github.com:443` 连接超时 / `schannel: failed to receive handshake`（TLS 握手失败）
- 诊断：`github.com` 被解析到 `198.18.0.13`（FlClash Fake-IP 网段），FlClash 在运行、端口 7890 开放，但直连、沙箱外、经代理端口四种路径均失败 → **FlClash 上游代理链路无法到达 github.com**
- 处置：切换 FlClash 节点 / 检查订阅 / 或临时关闭 DNS 劫持让 github 走直连后重推
- 影响：不阻塞 W1 开工（纯本地开发）

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

- 已入库并推送：`12a5f79` → `18b87f3` → `465f927` → `2f6eea5` → `fd6b1ee` → `8decced` → `a4bcdd1` → `c1a4b9e`（origin/main HEAD）
- **未推送**：`4789f3a`（docs(e1) E-1 盘点报告）—— 本地领先 origin 1 个提交，受 push 网络故障阻塞（见 §三）
- 工作区：**有未提交改动**（2026-10-15：D8/D9 文档整理 + E-1 W1/W2/W3 实施 + TRACE 修复，均未提交）—— 文档：`README.md`、`docs/README.md`、`docs/api.md`、`docs/architecture.md`、`docs/design/{special-projects-2026-09,rag-enhancement,chat-latency-and-model-config,agent-evolution,e1-navigable-lookup-gap,evolution-research-2026-09,improvement-roadmap,wiki-navigable-workspace,price-trustworthiness,intent-router-upgrade,search-optimization}.md`、`docs/guide/{deployment,development}.md`；代码：`.env.example`、`backend/src/{config.py,services/wiki_navigator.py,services/kb_retrieval_service.py,services/trace_collector.py,services/pipeline/{state,stages}.py,services/tools/plugins/wiki_lookup_tool.py}`、`backend/tests/{test_wiki_navigator,test_multi_query_retrieval,test_trace_ownership}.py`、`backend/scripts/run_wiki_multihop_ab.py`；归档重命名 `docs/design/direct-model-switch-plan.md` → `docs/archive/direct-model-switch-plan-2026-10-03.md`；新增未跟踪 `docs/project-status.md`（本台账）
