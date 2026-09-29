# 后续完善与优化执行计划

> 状态：部分实施（执行中；P0-1 / P1-1 / P1-2 / P1-3 / P2-3~P2-6 已完成，各条目以 ✅ 标记进度；遵循「设计先行 → 确认后实施」流程）
> 日期：2026-09-01
> 依据：对标企业级 RAG 项目（RAGFlow / Dify / FastGPT / MaxKB）与 2026 年生产级 RAG 架构共识，结合本项目现状盘点

## 1. 背景与现状小结

本项目已具备多数企业级 RAG 的对标组件：

- 混合检索（`hybrid_search.py`，BM25 + 向量）
- 重排序（`ollama_reranker.py`）
- 查询改写（`query_rewriter.py`）
- 引用补全（`citation_backfiller.py`）与数字幻觉校验（`numerical_validator.py`）
- 链路采集（`trace_collector.py`）与 Prometheus 监控告警
- 评估服务（`generation_evaluator.py` / `retrieval_evaluator.py`）与评估数据集（`tests/evaluation/kb_eval_dataset.jsonl`）
- 坏例反馈（`badcase/`）与学习引擎（`learning_engine.py`）
- 缓存（`cache_service.py`）、工具插件体系（`tools/plugins/`）

因此本计划的定位是「从有到强」，按投入产出比排序执行。

## 2. 执行顺序总览

| 批次 | 内容 | 优先级 | 依赖 |
|---|---|---|---|
| P0-1 | 评估闭环接入 CI（✅ 2026-09-01 完成） | 最高 | 无 |
| P0-2 | 深度文档解析 + 结构化分块（✅ 2026-09-01 完成） | 高 | 建议在 P0-1 之后（有度量基线） |
| P1-1 | JWT 多用户认证落地（✅ 2026-09-02 完成，含数据隔离复核） | 高 | 无 |
| P1-2 | 全链路 Trace 可视化（✅ 2026-09-02 完成） | 中 | 无（Trace 已带 user_id，查询 API 按用户过滤） |
| P1-3 | 语义缓存（✅ 2026-09-07 完成） | 中 | 无 |
| P2-1 | GraphRAG 检索增强（轻量版） | 中（2026-09-23 A/B 决策：维持挂起） | P0-1 |
| P2-2 | 多查询并行检索 + RRF 融合 | 中 | P0-1（已在 mq_eval_dataset 上离线 A/B，结论保持关闭，见 §P2-2 完成记录） |
| P2-3 | 引用溯源到原文高亮 | 中 | ✅ 已实施（此前 P0/P1 阶段落地，见 §P2-3 说明） |
| P2-4 | 审计日志 | 中 | ✅ 2026-09-22（见 §P2-4 完成记录） |
| P2-5 | 告警规则补齐 | 中 | ✅ 2026-09-22（见 §P2-5 完成记录；Alertmanager 通知渠道沿用文档设计决策） |
| P2-6 | 前端 E2E 核心链路（✅ 2026-09-06 完成，含前端单测体系） | 中 | 无 |
| P3 | 股价工具落地 / 学习引擎闭环验收 / 缓存盘点 / 重排序升级 / chunk 级权限 | 低 | 按需 |

## 3. 各项详细方案

### P0-1 评估闭环接入 CI（✅ 已完成 2026-09-01）

**完成记录**：
- 评估语料落地为 `tests/evaluation/eval_corpus.jsonl`（4 个 golden 文档 + 8 个干扰文档），评测不再依赖本地 Milvus 数据
- 新增 `scripts/run_eval.py`：复用生产 BM25 稀疏检索栈（pymilvus.model.sparse），离线输出 hit_rate@5 / mrr@5 / recall@5，低于阈值（环境变量 `EVAL_MIN_HIT_RATE` / `EVAL_MIN_MRR` / `EVAL_MIN_RECALL`，默认 0.8 / 0.5 / 0.5）非零退出
- 新增 `tests/evaluation/test_retrieval_quality.py` 纳入 unit job；CI 新增独立 `rag-eval` job（依赖 backend-unit）输出 eval_report.json 工件并卡点
- 本地基线：hit_rate/mrr/recall 均 1.0（5 问题全部第一命中）；阈值超标场景验证退出码 1
- 生成质量评估（faithfulness / relevance）仍为本地手动任务，未进 PR 卡点

**目标**：RAG 质量回归可度量、可卡点，避免每次改动问答质量靠手感。

**现状**：`tests/evaluation/` 下的场景测试（`test_rag_scenarios.py`、`test_retrieval_evaluator.py` 等）与 `verify_e2e.py` 为手动脚本性质，未纳入 `.github/workflows/ci.yml`。

**方案**：

1. 将 `tests/evaluation/` 中可离线运行的检索场景测试改造为标准 pytest 用例（mock LLM 生成环节，只评估检索链路），纳入现有 CI 的 unit test job。
2. 新增评估指标统计脚本：基于现有 `kb_eval_dataset.jsonl` 输出 context precision / recall 汇总。
3. 在 CI 中增加独立 job `rag-eval`（依赖 unit tests 通过），仅对检索指标做阈值卡点，行业标准参考：context precision > 0.8、context recall 按基线值设定。
4. 生成质量评估（faithfulness / relevance，依赖 LLM）保留为本地/定期手动任务，不进 PR 卡点（成本与稳定性考虑）。

**涉及文件**：`backend/tests/evaluation/`、`.github/workflows/ci.yml`、可能新增 `backend/scripts/run_eval.py`。

**验收标准**：CI 全绿且 `rag-eval` job 输出指标报告；人为降低检索质量时 CI 能红。

**风险**：CI 中无本地 Milvus 数据，需在 job 内灌入评估数据集或 mock 向量检索结果。

### P0-2 深度文档解析 + 结构化分块

**目标**：解决扫描版 PDF、复杂表格、跨页表格的解析失败；行业共识「RAG 大部分失败发生在分块」。

**现状**：`document_processor.py` 为基础文本抽取 + 固定分块。

**方案**（分两步）：

1. **结构化分块（✅ 已完成 2026-09-01）**：
   - Markdown/HTML/Word 按标题层级分块，chunk 携带 heading path（如 `[一级标题 > 二级标题]` 前缀），提升检索上下文。
   - 表格保持行级 chunk（行 + 表头）并附表格摘要，禁止跨行切割。
   - 代码块保持完整不切分。
   - 参数参考：长文本 500-800 token、overlap 50-100；技术文档按结构 200-1500 token、代码 0 overlap。

   **完成记录**：
   - Markdown：自研逐行扫描器（标题栈 + fenced 代码块保护 + 表格行级分块），超长正文二次切分并传播 heading_path；同时修复了 `.md` 原用 UnstructuredMarkdownLoader 剥离 `#` 标记导致标题切分失效的潜在 bug（改用 TextLoader + utf-8；注：txt/md/csv/json/pdf 加载器现已迁移为本地直读 `src/services/document_loaders.py`，此处为当时实施记录）
   - HTML：HTMLHeaderTextSplitter 切分 + 表格切分前占位符提取（裸占位符会丢失标题归属，用 `<p>` 包裹）+ 行级还原 + 正文二次切分
   - Word：UnstructuredWordDocumentLoader 改 `mode="elements"`，Title 元素经启发式标题栈构建层级路径（unstructured 不保留标题级别），Table 元素整块保留
   - Milvus schema 新增 `heading_path`（VARCHAR 512），旧集合经 `add_collection_field` 在线补加，失败自动降级不阻断；检索结果透出 heading_path
   - 验证：`tests/test_structured_chunking.py`（11 用例）+ `tests/evaluation/test_structured_vs_recursive.py`（4 用例：命中率/MRR 非退化、表格行问题第一名直接命中、命中 chunk 更短更聚焦）；全量 572 passed 无回归
   - 附带修复：`get_strategy` 对字符串策略输入的强制转换（原会 AttributeError）
2. **深度解析（✅ 已完成 2026-09-01）**：

   **完成记录**：
   - 选型：对比 MinerU / PaddleOCR / RapidOCR，结论为**双后端接入**（MinerU pipeline + PaddleOCR PP-StructureV3，均为 CPU 可跑的离线开源方案），RapidOCR 无版面分析排除；实测对比用 `scripts/compare_ocr_backends.py`
   - 架构：新增 `src/services/ocr_parser.py`——pypdf 字符密度检测扫描版（平均每页 < `OCR_SCANNED_CHAR_THRESHOLD`）→ 分流双后端 → 统一输出 Markdown → 复用 P0-2a Markdown 结构化分块（零新增分块代码）；chunk 元数据带 `ocr_backend` 溯源
   - 依赖：可选依赖组 `ocr-mineru`（mineru[pipeline]>=3.2）/ `ocr-paddle`（paddlepaddle + paddleocr + paddlex[ocr]，paddleocr 3.x 不自带 paddle 框架，PP-StructureV3 需 paddlex[ocr] 附加依赖），默认不安装、CI 不装；未安装/解析失败自动回退内置 PyPDF 解析，不阻断上传。为容纳 mineru/paddlex 的依赖约束，主依赖放宽两处：`huggingface_hub>=0.34,<2`（原 ==1.18.0，mineru 需 transformers 4.x → hub<1.0）、`PyYAML>=6.0.2,<7`（paddlex 锁 ==6.0.2）；锁内全局降为 hub 0.36.2 / transformers 4.57.6（ST 5.5.1 声明兼容）
   - 调用：mineru 走 CLI 子进程（隔离 torch 环境、`MINERU_TIMEOUT_SECONDS` 可控超时、显式 utf-8 防 GBK 乱码）；paddle 走进程内 Python API（pipeline 实例全局复用避免重复加载模型）；`OCR_DEVICE=auto|cpu|cuda` 控制推理设备（4GB 显存可走 GPU 加速，MinerU pipeline 最低要求即 4GB）
   - 脚本：`download_ocr_models.py`（生成样例扫描件跑一次两后端 → 模型预热 + 冒烟验证）、`compare_ocr_backends.py`（双后端独立子进程对比：耗时/字符数/标题数/表行数/基准相似度）、`batch_reparse.py`（存量扫描 PDF 迁移：调用现有 reprocess 接口批量重解析；关闭 `DEEP_PARSING_ENABLED` 后运行同一脚本即回退）
   - 配置：`OcrSettings`（`DEEP_PARSING_ENABLED` / `OCR_BACKEND=auto|mineru|paddle` / `OCR_DEVICE` / 检测阈值 / 超时 / 模型源），`.env.example` 已同步
   - 验证：`tests/test_ocr_parsing.py` 20 用例（全 mock，CI 可跑：检测/分流/回退/Markdown 策略自动切换/元数据传播）+ `tests/evaluation/test_ocr_backend_comparison.py`（双后端 e2e，无后端环境自动跳过）；全量单测无回归
   - 双后端预热冒烟：两后端均通过（modelscope 模型源，MinerU pipeline 模型 ~1.5GB / PP-StructureV3 模型 ~数百 MB）
   - 实测对比（样例中文扫描件 1 页，CPU，耗时含模型初始化 ~85s、单页推理仅数秒）：mineru 90.1s / 88 字 / 2 表行 / 相似度 0.8235；paddle 110.6s / 82 字 / 0 表行 / 相似度 0.8376。文本抽取质量相当；mineru 表格还原更好、初始化更快，paddle 部署更轻。样例过小，正式结论需用真实扫描件复测（`compare_ocr_backends.py <真实扫描件.pdf> --ground-truth ref.txt`）
   - 踩坑：paddle 3.x Windows CPU 推理需 `enable_mkldnn=False`（否则 `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support`，已在 ocr_parser 与预热脚本中禁用）；模型缓存默认写用户主目录，可经 `MODELSCOPE_HOME` / `MODELSCOPE_CACHE` / `PADDLE_PDX_CACHE_HOME` 重定向

**涉及文件**：`backend/src/services/ocr_parser.py`（新增）、`document_processor.py`（PDF 分流 + 策略切换）、`config.py`（OcrSettings）、`scripts/{download_ocr_models,compare_ocr_backends,batch_reparse}.py`、`pyproject.toml`（可选依赖组）。

**验收标准**：扫描版 PDF（含标题/表格）经两后端解析后可正常检索问答（本地 e2e 对比测试覆盖）。

**风险**：分块策略变更需重建向量库（需写迁移/重建脚本并保留旧索引回退能力）。→ 实际通过在线加字段规避：heading_path 仅作附加信息，旧数据该字段为空串，无需重建向量库；如需为旧文档补齐 heading_path 可重新触发重处理。

### P1-1 JWT 多用户认证落地（✅ 已完成 2026-09-02）

**目标**：打通数据层已预留的多用户能力（`owner_id`）。

**方案**：

1. 引入 `python-jose`（或 PyJWT）实现 JWT 签发/校验，密钥走 `SECRET_KEY` 配置。
2. 新增用户注册/登录接口与用户表（复用现有 PostgreSQL 迁移脚本机制）。
3. `get_current_user` 按 API Key（保留兼容）→ JWT → 开发模式默认用户顺序校验；JWT 用户 id 写入 `owner_id` 链路，实现知识库/文档/会话按用户隔离。
4. 前端增加登录页与 token 管理（axios 拦截器 + 路由守卫）。

**验收标准**：多用户各自只能看到自己的知识库与会话；API Key 模式回归不受影响；CI 全绿。

**完成记录**：

1. 后端：`pyjwt` + `bcrypt`；`users` 表 + `/api/auth/register|login|me`；WS 首帧 token 鉴权；校验顺序 API Key → JWT → 开发模式匿名。
2. 前端：`LoginView` 登录/注册页、路由守卫、Pinia store、axios Bearer 注入与 401 拦截、WS auth 帧。
3. **数据隔离复核结论（2026-09-02）**：KnowledgeBase / Document / Session / Feedback / Badcase 均有归属字段 + 列表过滤 + 对象级 `require_owner`；聊天入口 `kb_ids` 经 `validate_kb_ownership` 校验——隔离完整。
4. **复核发现的缺口已修复**：`RequestTrace` 此前落库 `user_id`/`session_id` 全为 NULL（[rag_chain.py] `_pipeline` 未透传），已改为 chat 非流式/流式入口透传当前用户与会话，Trace 归属随记录持久化（`tests/test_trace_ownership.py`）。P1-2 的 Trace 查询 API 必须按 `user_id` 过滤。
5. **产品设计决策**：Category / Tag 保持全局共享（系统级资源，不加 owner_id）；evaluation / experiment / learning / config 为系统级接口，暂维持现状，后续如需收紧可挂 `require_admin`。

### P1-2 全链路 Trace 可视化（✅ 已完成 2026-09-02）

**目标**：补齐可观测性第四维度——按单次问答查看检索→重排→生成各步耗时与内容（对标 Langfuse 体验）。

**现状**：`trace_collector.py` 已采集，`request_trace` 模型已落库，但无查询界面。

**方案**：

1. 新增 Trace 查询 API（按会话/时间过滤，返回分步明细）。
2. 前端新增 Trace 查看页（或在会话详情中嵌入），以时间线展示各阶段（意图路由/改写/检索/重排/工具调用/生成）的耗时与中间产物。
3. 补充每步 token 用量记录（成本可观测）。

**涉及文件**：`backend/src/api/`（新端点）、`trace_collector.py`、前端新增视图。

**完成记录**：

1. 模型/迁移：`request_traces` 新增 `stages`（分阶段耗时 JSON）与 `token_usage` 列，迁移脚本 `scripts/migrate_traces.py`（幂等，ADD COLUMN IF NOT EXISTS）。
2. 采集：`TraceCollector.add_stage` / `set_token_usage`；`_pipeline` 用 `_StageTimer` 对上下文增强/意图路由/工具调用/知识库决策/Agent/联网搜索/知识库检索/生成各阶段计时；token 用量优先取 Ollama `response_metadata`（prompt_eval_count/eval_count），缺失时用 `estimate_token_count` 字符估算并标记 `estimated=true`。
3. API：`GET /api/traces`（分页列表，按 `current_user.user_id` 隔离，默认用户额外可见 NULL 归属历史记录；支持 session_id/时间范围过滤）、`GET /api/traces/{id}`（详情，跨用户 404）。
4. 前端：`/traces` 全局页（侧边栏「链路追踪」入口）+ 历史对话页会话操作栏「链路追踪」按钮（携带 session_id 跳转）；详情抽屉以 el-timeline 展示阶段耗时、检索命中、工具调用与最终答案，token 用量带「估算」标记。
5. 测试：`tests/test_trace_api.py`（过滤条件/列表序列化/详情 404/NULL 归属可见性）+ `tests/test_trace_ownership.py` 扩展（add_stage/set_token_usage）。
6. **注意**：部署时需执行 `cd backend && uv run python scripts/migrate_traces.py`。

### P1-3 语义缓存（✅ 已完成 2026-09-07）

**目标**：相似问题命中缓存，降本提速。

**现状**：~~`cache_service.py` 为精确匹配缓存~~（实施前盘点修正：项目实际**没有答案级缓存**，`cache_service.py` 仅服务联网搜索与 KB 列表缓存；详见设计文档 §1）。

**方案**：在精确命中未命中后，计算查询 embedding 与缓存问题向量做相似度匹配（阈值可配，默认 0.92），命中则返回缓存答案并标记「来自相似问题」。仅对纯知识库问答启用，工具调用/时效性问题不缓存（沿用现有时间敏感判断）。

**风险**：相似但不等价的问题返回错误缓存；通过阈值 + 仅知识库域 + 后台过期刷新控制。

**完成记录**（设计详情见 `docs/design/semantic-cache.md`）：

1. 新增 `src/services/semantic_cache_service.py`：`lookup`（精确匹配快路径 + 余弦语义匹配）/ `store` / `invalidate_kb` / `schedule_invalidation`；Redis HASH 按 scope 存储（key 含 user_id 与 kb_scope，跨用户不共享），TTL 惰性过滤 + 单 scope 200 条容量淘汰，Redis 异常 fail-open。
2. `rag_chain.py` 管线插桩：KB 决策后缓存查找，命中发 `cache_hit` reasoning 步骤并流式回放缓存答案后短路（不触发检索与生成，零 token 消耗）；finalize 前仅对纯 KB 答案（knowledge_base 类型、无联网来源、deep_thinking=off、非时效性、有来源文档）异步后台写入。
3. 失效钩子：文档上传/删除/重建与知识库删除/批量删除后 `invalidate_kb`（匹配该 kb_id 或 all 全域条目）。
4. 配置 `SemanticCacheSettings`（5 项）入 `.env` / `.env.dev` / 两份 compose；Prometheus 新增 hits/misses/stores 计数与 lookup 耗时直方图；Trace 落 `semantic_cache_hit` 数据。
5. 测试：服务级 + 管线级共 45 个用例；全套 688 passed / 100 skipped 无回归。

### P2-1 GraphRAG 检索增强（轻量版）

**前置进展（2026-09-08 更新）**：共享实体抽取环节的 **LLM-Wiki 编译层 Phase 1（2026-09-07）与 Phase 2 级联/API/前端抽屉（2026-09-08）已完成**（编译页作为补充语料混入混合检索，默认关闭，变更待提交；见 `docs/design/llm-wiki-compile.md` §8/§10，运行时验证见 §12）。Phase 3 设计稿（覆盖先验/链接扩展/Lint）见 §11 待确认。按 §2.3 约定，P2-1 启动前需先评估编译页是否已解决跨文档推理问题——若已解决，P2-1 降级或合并。

**决策记录（2026-09-23，跨文档多跳检索 A/B，维持挂起）**：

- 工具：`backend/scripts/run_wiki_multihop_ab.py` + 数据集 `backend/tests/evaluation/wiki_multihop_eval_dataset.jsonl`（8 个需联合 ≥2 篇文档事实的跨文档问题，配套离线单测 `test_wiki_multihop_ab.py`），逻辑校验入 CI unit job。
- 三臂结果（BM25 离线代理，top-5，编译模型 qwen3:4b-instruct-2507-q4_K_M，编译 12 篇语料产出 36 页）：
  ```
  A(仅原始chunk)      hit@5=1.000  mrr@5=1.000  recall@5=0.938
  B(原始chunk+wiki)   hit@5=1.000  mrr@5=1.000  recall@5=0.875
  C(仅wiki页,诊断)    hit@5=1.000  mrr@5=1.000  recall@5=0.875
  ```
- 解读：混入编译页对跨文档问题无增益（recall -0.062），编译页自身也未覆盖跨文档关联——per-doc 编译页不含文档间桥接信息，符合预期。
- 局限（诚实声明）：① 语料仅 12 篇短文档，基线已近天花板（recall 0.938，8 题仅漏 1 个 golden 文档），增益空间本身很小；② 离线代理未建模管线内的 `WIKI_LINK_EXPANSION` 链接扩展阶段，B 臂是生产机制效果的下界。
- **决策：P2-1 维持挂起，不启动轻量 GraphRAG**。理由：现有证据下投入产出比不足；Wiki 混入保持"非退化"底线（test_wiki_ab.py）。触发重启的条件：线上 trace/badcase 出现真实的跨文档问题占比（而非合成数据），或未来语料规模显著增长后复测本 A/B。
- **收编决策（2026-09-29，本项正式关闭）**：由 [evolution-research-2026-09.md](evolution-research-2026-09.md) 四阶段路线收编——阶段一「导航式 wiki + Query 回流」以更低成本解决同一跨文档缺口；GraphRAG 降为阶段四图记忆可选件。执行立项见 [special-projects-2026-09.md](special-projects-2026-09.md) §E。

**现状**：`knowledge_graph_generator.py` 已生成知识图谱，但仅用于前端展示，未参与检索。

**方案**：检索阶段增加实体链接步骤 → 在图谱中取实体一跳邻居与关系描述 → 作为补充上下文并入召回（不替代向量检索）。仅在意图路由判定为实体/关系类问题时启用。成效依赖 P0-1 评估验证。

### P2-2 多查询并行检索 + RRF 融合

**方案**：`query_rewriter` 改写出 3~5 个查询并行召回，RRF 合并去重。注意行业实测：有重排序兜底时多查询增益缩水，必须先在评估基线上对比单查询基线，确认有增益再合入。

**完成记录（2026-09 评估收尾，结论：保持关闭，有据决策）**：
- 新增全离线决策工具 `backend/scripts/run_multiquery_ab.py` + 数据集 `tests/evaluation/mq_eval_dataset.jsonl`（16 样本含 `query_variants`，不动 `kb_eval_dataset.jsonl` 的 CI 卡点）+ 单测 `test_multiquery_ab.py`（6 用例，仅校验逻辑不设增益卡点）。
- RRF 口径与生产 `reciprocal_rank_fusion` 一致（k=`KB_RRF_K`=60、`channel_top_k`=`KB_MULTI_QUERY_CHANNEL_TOP_K`=10）。
- 实测（BM25 离线，top-5，13 个语义正常问题 + 3 个改写敏感难题）：
  ```
  A(单查询)      hit@5=0.938  mrr@5=0.833  recall@5=0.938
  B(多查询+RRF)  hit@5=0.938  mrr@5=0.825  recall@5=0.938
  ```
  hit/recall 无增益，mrr 反降；改写敏感难题 #16 单查询 miss 且多查询也救不回——实证"有重排序兜底时多查询增益缩水"。
- **决策**：`KB_MULTI_QUERY_ENABLED=false` 保持关闭（本地单机还放大 N× 延迟）。如需复开，先在真实 QueryRewriter 改写下于同样本上复测有增益再合入。

### P2-3 引用溯源到原文高亮

**方案**：引用信息中补充 chunk 定位数据（页码/偏移/heading path），前端 `DocumentPreviewDialog` 按引用定位并高亮原文片段（对标 RAGFlow 的可追溯答案）。

**说明（2026-09 复核）：端到端已实现**。后端 `_docs_to_sources` 产出 `document_id`+`chunk_index`+`total_chunks`（rag_chain.py），`GET /documents/{doc_id}/source/{chunk_index}` 返回 `content/surrounding_content/highlight_offset/highlight_length`（api/document.py）；前端 MarkdownRenderer 注入 `[N]` → ChatMessageList → ChatView → `DocumentSourceModal` 调用该接口并用 `<mark>` 高亮命中片段。此前 P0/P1 阶段已落地，仅状态未回写。

### P2-4 审计日志

**方案**：新增 `audit_log` 表，记录敏感操作（文档上传/删除、知识库增删、配置变更、用户管理），与调试性质的 `request_trace` 分离；提供按操作者/时间过滤的查询接口。

**完成记录（2026-09-22）**：
- 模型 `src/models/audit_log.py`（`JSONType` JSON/JSONB variant，同 request_trace；`create_all` 启动自动建表，无需迁移脚本）。
- 服务 `src/services/audit_service.py`：`record_audit`（best-effort，异常永抛不阻断主流程）+ `query_audit_logs`（分页/计数/过滤）。
- API `src/api/audit.py`：`GET /api/audit`（`require_admin`），按 action/resource_type/resource_id/user_id/时间过滤 + 分页；`main.py` 已挂载。
- Hook 接入：用户注册（auth.register）、配置改/重置（config.update/reset）、知识库创建（kb.create）、文档上传/删除（document.upload/delete）——均 best-effort 记录操作者。
- 单测 `tests/test_audit.py`（4 用例，FakeDb 内存态）。
- 附带修正：`GET /config/info` 的 `processing_defaults` 由陈旧的 500/50 对齐为默认 512/64。

### P2-5 告警规则补齐

**方案**：在 `configs/prometheus/alerts.yml` 补充关键链路告警：Milvus 连接失败、SSE 流式中断率、Ollama 请求超时率、检索失败率；配置 Alertmanager 通知渠道。

**完成记录（2026-09-22）**：
- `alerts.yml` 新增 3 条规则：`RetrievalFailureRate`（检索失败率，`rag_retrieval_errors_total`）、`SseStreamInterruptRate`（SSE 流中断，`rag_sse_interrupted_total`）、`OllamaTimeoutRate`（Ollama 超时/失败率，复用 `rag_llm_calls_total/rag_llm_call_errors_total`）。
- Milvus 连接失败由既有 `MilvusDown`（`up{job="milvus"}==0`，30s）覆盖，未重复建规则。
- 新增 2 个计数器埋点（均 best-effort，不阻断主流程）：`rag_retrieval_errors_total`（kb_retrieval_service：混合失败且 dense 兜底也失败时）；`rag_sse_interrupted_total`（rag_chain `arun_stream` 用 `except GeneratorExit` 捕获客户端中途断连，正常完成不触发）。
- 校验测试 `tests/test_prometheus_alerts.py`（3 用例：结构合法 / alert 名唯一 / 新指标与 prometheus.py 定义一致，防拼写漂移）。
- **Alertmanager 通知渠道**：沿用既有 `configs/alertmanager/alertmanager.yml`，webhook 接收端 URL 为空需部署时按文档/环境注入（P1-E 已定"webhook 静默跳过、通知渠道留待部署"），本项不再改动。

### P2-6 前端 E2E 核心链路

**方案**：基于现有 Playwright，补三条核心用例：上传文档→解析完成→提问命中知识库→引用展示；会话切换与历史查看；知识库增删。

### P3 低优先级（按需启动；2026-09-29 收编调整）

- 股价工具落地（设计文档 `price-trustworthiness.md` 2.2.3 已选型）。
- ~~学习引擎「反馈→规则→检索策略」端到端闭环验收~~ → **并入 evolution 阶段一 E-2「Query 回流」**（同源合并实施，见 [special-projects-2026-09.md](special-projects-2026-09.md) §E.2）。
- 缓存策略盘点（文档列表、会话消息等高频路径覆盖情况）。
- 重排序模型升级（评估 bge-reranker 等专用模型对比 ollama 方案）。
- ~~chunk 级权限（多部门共用知识库时再做）~~ → **归入 evolution 阶段三「Context Engine 收口」**（统一检索 API 的权限过滤层一并实施）。

## 4. 执行约定

- 每一项启动前按「设计先行 → 确认后实施」流程执行，本文件为总体路线图。
- 单项实施时输出具体改动清单与测试结果，CI 保持全绿。
- 所有新依赖必须开源可离线（禁用付费 API）。
- 涉及索引/数据结构的变更（如 P0-2）必须提供迁移与回退方案。
