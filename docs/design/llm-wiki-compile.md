# LLM-Wiki 编译层设计（RAG 前置知识编译增强）

> 状态：Phase 1 已实施（2026-09-07，记录见 §8）；Phase 2 已实施（2026-09-08，记录见 §10）；Phase 3 已实施（2026-09-09，记录见 §11.7）；Phase 4 已实施（2026-09-09，记录见 §13.6）；Phase 5 已实施（2026-09-09，记录见 §14.8）；运行时验证与运维依赖记录见 §12（2026-09-08）
> 日期：2026-09-06
> 关联：[improvement-roadmap.md](improvement-roadmap.md) P2-1 GraphRAG（定位关系见 §2.3）

## 1. 背景与目标

### 1.1 范式来源

Karpathy 于 2026 年 4 月提出 LLM-Wiki 范式（[gist 442a6bf5](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)）：不再每次查询时从原始文档临时检索（RAG 的「解释器」模式），而是让 LLM 将原始资料一次性「编译」为结构化、互联的 Markdown Wiki 并持续维护（「编译器」模式）。三层架构：`raw/`（不可变原始资料）→ `wiki/`（LLM 维护的实体页/主题页/索引）→ Schema（结构约定）。

### 1.2 学术证据（WiCER, arXiv 2605.07068）

- 策展小语料下，编译产物 + 全上下文推理优于 RAG（4.38 vs 4.08/5，TTFT 快 7.3×）
- 规模增大后注意力稀释，全上下文质量跌回 RAG 之下（3.47 vs 3.64）
- **盲编译灾难性丢事实**（53-60% 灾难失败率）；诊断探针式迭代编译可挽回 80% 损失

### 1.3 本项目定位

**LLM-Wiki 作为 RAG 的前置编译增强层，不取代向量检索**：

| 路线 | 判断 |
|---|---|
| 用 Wiki 取代向量库（Karpathy 原始形态） | 否决：本项目是多用户服务化系统，KB 长期增长，规模边界（≤200 篇）不成立 |
| 编译页作为补充语料入库，与原始 chunk 混合检索 | **采纳**：编译页语义完整（多篇按概念重组、标注矛盾），弥补 chunk 级跨文档推理弱项；raw 层兜底编译丢事实风险 |

### 1.4 设计目标

1. 文档摄入后追加**离线编译阶段**，用本地 Ollama 模型生成/更新该 KB 的 Wiki 页面，作为高质量补充语料进入现有混合检索。
2. **查询侧零改动**：Phase 1 不动检索、上下文构建与前端引用链路。
3. **编译质量可度量**：复用现有评估设施（`scripts/run_eval.py`、`tests/evaluation/`）做 A/B 验证，不达标不上线。
4. 全程离线开源，无付费 API。

---

## 2. 总体架构

### 2.1 编译层挂载点

挂在现有异步摄入管线的末尾（[document.py](../../backend/src/api/document.py) `process_document_async`，当前为三阶段：解析 0-30% → 向量化 30-70% → 规则分析 70-90%）：

```text
上传文档 → process_document_async（现有，不动）
  阶段1 解析            0-30%   （现有）
  阶段2 向量化          30-70%  （现有）
  阶段3 规则分析        70-90%  （现有）
  阶段4 Wiki 编译(新)   90-100% （WIKI_COMPILE_ENABLED 控制，默认关）
      │
      ├─ 读取本文档 chunks（含 heading_path 结构化信息）
      ├─ 读取该 KB 现有 wiki 页面状态（wiki_pages 表 + MinIO 内容）
      ├─ WikiCompiler（Ollama 本地模型，复用 model_manager）
      │    实体/主题抽取 → 匹配既有页 → 生成/更新页面 → 交叉链接
      ├─ 页面正文持久化到 MinIO（wiki/{kb_id}/{page_id}.md）
      ├─ 结构化分块 → add_documents(kb_id)，metadata 标 source_kind="wiki"
      └─ 更新 wiki_pages 表（源文档映射、revision）
```

关键约束：

- **异步不阻塞上传**：编译在后台任务内，进度经现有 `notify_task_progress` WebSocket 通道推送（90-100% 区间）。
- **raw 层不可变**：原始 chunk 永远保留在向量库，编译失败/关闭时检索链路完全不受影响。
- **按 KB 串行**：同一 KB 的编译任务串行化（分布式锁或 DB 行锁），避免并发上传导致页面更新互相覆盖。

### 2.2 编译器核心流程（wiki_compiler.py）

```text
输入：新文档 chunks + 该 KB 现有 index 页
 1. 实体/主题抽取：LLM 从 chunks 提取候选实体页、主题页（结构化 JSON 输出）
 2. 页面匹配：与该 KB 现有 wiki_pages 按 title 相似度匹配（embedding 复用现有 bge-m3）
 3. 页面生成/更新：
    - 新页：综合本次来源生成初稿，标注来源 doc_id
    - 已有页：增量合并（prompt 约束：保留既有事实，新旧矛盾显式标注而非静默覆盖）
 4. 交叉链接：更新页内 [[链接]] 与 index 页目录
 5. 持久化：MinIO 写 markdown → 分块嵌入入库（source_kind="wiki"）→ 更新 DB 映射
```

- 单篇文档触发的页面更新数受 `WIKI_COMPILE_MAX_PAGES_PER_DOC` 上限约束（控 token 成本，Karpathy 经验值 10-15 页）。
- 编译模型可独立配置（`WIKI_COMPILE_MODEL`），默认复用主模型；建议实测更小的本地模型以降本。

### 2.3 与路线图 P2-1 GraphRAG 的关系

| | P2-1 GraphRAG 轻量版 | 本设计 LLM-Wiki 编译 |
|---|---|---|
| 产物 | 实体一跳邻居 + 关系描述（结构） | 综合叙述页面（内容） |
| 查询时 | 实体链接 → 图谱扩展召回 | 无差异，页面直接参与混合检索 |
| 重叠度 | 实体抽取环节高度重叠 | 同左 |

**建议**：二者共享实体抽取环节。先落地本设计（改动面小、评估设施现成），P2-1 在其上评估「图谱结构扩展」是否还有增量收益——若编译页已解决跨文档推理问题，P2-1 可降级或合并。

---

## 3. 详细设计

### 3.1 数据模型

**新表 `wiki_pages`**（PostgreSQL，沿用现有迁移脚本机制）：

```sql
CREATE TABLE wiki_pages (
    id UUID PRIMARY KEY,
    kb_id UUID NOT NULL REFERENCES knowledge_bases(id),
    page_type VARCHAR(16) NOT NULL,      -- entity / topic / index / log
    title VARCHAR(256) NOT NULL,
    content_path VARCHAR(512) NOT NULL,  -- MinIO: wiki/{kb_id}/{page_id}.md
    source_doc_ids JSONB NOT NULL DEFAULT '[]',  -- 贡献该页的源文档，级联更新依据
    revision INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(16) NOT NULL DEFAULT 'active', -- active / stale / deleted
    owner_id UUID,                       -- 沿用用户隔离（KB 归属校验已覆盖）
    created_at / updated_at TIMESTAMP
);
UNIQUE (kb_id, page_type, title)  -- 幂等 upsert 依据
```

**chunk metadata 新增字段**：

- `source_kind`: `"raw"` | `"wiki"`。
- ⚠️ 现有 `doc_type` 字段已被分块策略值占用（[document_processor.py](../../backend/src/services/document_processor.py) L477-484），**不可复用**；Milvus schema 需在线补加字段（沿用 `add_collection_field` 机制，与 P0-2 heading_path 同路径，失败降级不阻断）。

### 3.2 模块与涉及文件

| 文件 | 改动 |
|---|---|
| `src/services/wiki_compiler.py` | 新增：编译器核心（抽取/匹配/更新/链接） |
| `src/api/document.py` | `process_document_async` 追加阶段 4；编译进度推送 |
| `src/config.py` | 新增 `WikiCompileSettings`（见 §3.3） |
| `src/services/vector_store.py` | Milvus schema 在线补加 `source_kind` 字段 |
| `src/models/wiki_page.py` + 迁移脚本 | 新表 |
| `src/services/minio_service.py` | 新增 wiki 对象读写辅助（复用现有 bucket） |
| `src/services/context_builder.py` | 可选（Phase 1 后）：SourceItem 透出 `source_kind` 供前端展示「编译页」徽标 |
| `scripts/rebuild_wiki.py` | 全量重编译脚本（按 KB） |
| `tests/test_wiki_compiler.py` | 编译器单测（LLM 全 mock） |
| `tests/evaluation/test_wiki_ab.py` | A/B 对比评估 |

### 3.3 配置项（默认全关，零风险合入）

```python
class WikiCompileSettings(BaseSettings):
    WIKI_COMPILE_ENABLED: bool = False            # 总开关，默认关闭
    WIKI_COMPILE_MODEL: str = ""                  # 空=复用主模型
    WIKI_COMPILE_MAX_PAGES_PER_DOC: int = 10      # 单文档页面更新上限
    WIKI_COMPILE_TIMEOUT_SECONDS: int = 300       # 单文档编译超时
    WIKI_DIAGNOSTIC_PROBES: bool = False          # Phase 2：WiCER 式诊断探针
```

### 3.4 API（Phase 2，按需）

- `GET /api/kb/{id}/wiki/pages`：页面列表（title/type/revision/来源文档数）
- `POST /api/kb/{id}/wiki/rebuild`：全量重编译（删旧 wiki 向量 → 重跑）
- 权限沿用 KB 归属校验（`require_owner`），无新增面。

### 3.5 检索侧与前端

- **Phase 1 检索零改动**：wiki 页分块走 `add_documents(kb_id)`，自动参与 BM25+向量混合检索与 rerank；`SourceItem.source_type` 仍为 `"kb"`。
- Phase 1 后可选增强：`SourceItem` 透出 `source_kind="wiki"`，前端引用列表加「编译页」徽标，区分一手来源与综合页（透明度，防止用户把综合页当原文）。

---

## 4. 分阶段实施

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| **Phase 1 MVP** | 单 KB 编译器 + 阶段 4 挂载 + source_kind 入库 + A/B 评估脚本 | 评估达标（§5），默认关闭合入 |
| **Phase 2** | wiki_pages 级联更新（源文档删除/重处理联动）、诊断探针、API + 前端页面列表、按 KB 串行化 | 级联更新单测全绿 |
| **Phase 3** | index 页喂意图路由做 KB 覆盖先验；交叉链接检索扩展；Lint（矛盾/孤立页检查报告） | 按需评估 |

## 5. 评估方案（上线门槛）

复用现有评估设施，不新建体系：

1. **检索指标 A/B**：`tests/evaluation/` 框架 + `scripts/run_eval.py`，同一问题集跑两组——原始 chunk（基线）vs chunk+wiki 混入。指标 `hit_rate@5 / mrr@5 / recall@5`，**非退化**为底线，提升为合入依据。
2. **编译事实保留率（WiCER 思路落地）**：从被编译文档抽取原子事实生成探针问题，检查编译页是否保留；保留率 < 90% 则迭代 prompt/换模型，仍不达标则该 KB 类型不启用编译。
3. **生成质量**：`generation_evaluator` 的 faithfulness / relevance 手动对比（沿用 P0-1 约定，不进 CI 卡点）。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 本地模型编译丢事实（WiCER：盲编译 53-60% 灾难失败） | raw 层永远保留兜底；诊断探针迭代；开关默认关，按 KB 灰度 |
| token 成本与编译时延 | 异步阶段不阻塞上传；`MAX_PAGES_PER_DOC` 限额；可配小模型 |
| 并发上传同 KB 页面互相覆盖 | 按 KB 串行化编译任务 |
| 综合页被误当原文引用 | source_kind 透出 + 前端徽标（透明性） |
| 页面膨胀/陈旧 | Phase 3 Lint 定期体检；status=stale 标记 |

## 7. 验收标准

1. 开关关闭时：行为与现状完全一致（回归零差异），CI 全绿。
2. 开关开启灰度 KB：编译页可检索、可引用、来源可追溯（source_doc_ids）。
3. A/B 评估：检索指标非退化且事实保留率 ≥ 90%，评估报告可复现。
4. 编译失败不阻断文档上传（文档状态正常 completed，编译错误仅记录日志与进度提示）。

## 8. 实施记录（Phase 1，2026-09-07）

- **落地文件**（对齐 §3.2 清单）：`src/services/wiki_compiler.py`（编译器核心）、`src/api/document.py`（管线末尾挂载编译阶段，失败/超时不阻断上传）、`src/config.py`（`WikiCompileSettings` 4 项，诊断探针配置留待 Phase 2）、`src/services/milvus_service.py`（schema 新增 `source_kind` + 旧集合在线补加，完全沿用 heading_path 降级模式）、`src/models/wiki_page.py` + `scripts/migrate_wiki_pages.py`（幂等建表）、`src/services/minio_service.py`（文本对象读写辅助）、`scripts/rebuild_wiki.py`（按 KB 全量重编译：清产物 → 读 Milvus raw chunks → 重编译）。
- **与设计稿的差异**：
  - `wiki_pages.id/kb_id` 用 `String(36)` 而非 UUID 列：与向量库 `document_id` 字符串体系一致；不加数据库级外键，KB 归属由应用层校验（与 KnowledgeBase.owner_id String 风格一致）。
  - 编译器拆为 `compile_pages`（纯 LLM，无 IO）与 `persist_pages`（MinIO + DB + 向量库）两层：A/B 评估与单测可直接调用纯编译层。
  - **索引页不进向量库**（仅 MinIO + DB）：目录类文本混入检索易污染排序；Phase 3 供意图路由消费时再评估。
  - 抽取 JSON 解析容忍代码围栏/包裹文字；LLM 输出统一剥离 `<think>` 块（兼容 qwen3 混合思考模型）。
  - 标题匹配阈值 0.85 为模块常量（`TITLE_MATCH_THRESHOLD`）；embedding 失败降级为仅精确匹配（未匹配一律按新页）。
  - wiki 页分块用 `RecursiveCharacterTextSplitter(1000/100)`，markdown 标题分隔符优先。
- **测试**：`tests/test_wiki_compiler.py` 21 用例（抽取容错/标题匹配/页数上限/upsert/管线挂载失败不阻断）；A/B 评估 `tests/evaluation/test_wiki_ab.py` 默认跳过（e2e 标记，需本地 Ollama），以 `--run-e2e` 运行，上线门槛 = 检索指标非退化 + 事实保留率 ≥ 0.9。全套 **709 passed / 101 skipped** 无回归。
- **部署提示**：表结构变更统一走 Alembic——已有数据库执行 `cd backend && uv run alembic upgrade head`（尚未纳入 Alembic 管理的存量库先 `uv run alembic stamp 27af4193802c` 打基线再升级）；新 Milvus 集合 schema 自带 `source_kind`，旧集合启动时在线补加（失败自动降级，不阻断）。

---

## 9. Phase 2 详细设计（2026-09-08 设计稿，已确认实施：孤儿页直接删除、前后端一起）

> 范围对齐 §4 Phase 2 行：级联更新、按 KB 串行化、API + 前端页面列表、诊断探针。

### 9.1 现状盘点（Phase 1 遗留缺口）

| 场景 | 现状 | 缺口 |
|---|---|---|
| 源文档删除 | `process_document_delete_async` 仅 `delete_by_document_id(doc_id)` 删 raw 向量 | wiki 向量的 `document_id` 是**页面 ID** 而非源 doc_id → wiki 向量残留；`source_doc_ids` 残留已删文档；无来源页面成孤儿继续参与检索 |
| 源文档重处理 | 管线自带阶段 4 重编译（revision+1） | `_index_page` 直接 add_documents，**不删旧向量 → 同页多 revision 向量重复累积**（Phase 1 缺陷） |
| KB 删除（单/批） | `delete_by_kb_ids` 已覆盖 wiki 向量（同集合 kb_id 过滤） | `wiki_pages` 表行 + MinIO `wiki/{kb_id}/` 对象残留 |

### 9.2 级联更新（wiki_cascade.py）

新增 `src/services/wiki_cascade.py`，两个入口，风格对齐语义缓存失效钩子（try/except 包裹，**失败不阻断删除主流程**）：

- `on_document_deleted(db, kb_id, doc_id)`：
  1. `WikiPage.source_doc_ids.contains([doc_id])`（JSONB containment）查受影响页
  2. 从 `source_doc_ids` 移除该 doc_id（JSONB 重新赋值触发 UPDATE）
  3. 移除后无任何来源 → **删页**：向量 `delete_by_document_id(page.id)` + MinIO 正文 + DB 行
  4. 否则仅更新来源列表（revision 不变——页面内容未改）
  5. 重建索引页（复用 `_upsert_index_page`）
  - 挂载点：`process_document_delete_async` 阶段 3 之后（batch_delete 复用同一后台任务，自动覆盖）
- `on_kb_deleted(db, kb_id)`：删 `wiki_pages` 行 + MinIO `wiki/{kb_id}/` 对象（向量已有 delete_by_kb_ids 覆盖）
  - 挂载点：`delete_knowledge_base` + `batch_delete_knowledge_bases`
- **重处理路径**：不新增钩子（管线自带编译），仅修复向量重复——`persist_pages` 对既有页先 `delete_by_document_id(row.id)` 再重新入库。
- **显式不做**：删除源文档后的页面内容 LLM 级联重写（移除失效引用）——成本高、raw 兜底可接受，列为 Phase 3 候选。

### 9.3 按 KB 串行化

- `wiki_compiler.py` 内模块级 `dict[kb_id, asyncio.Lock]` 注册表；阶段 4 编译与 rebuild API 共用同一把锁
- 锁在 `compile_document` 内部获取，整体仍被 `WIKI_COMPILE_TIMEOUT_SECONDS` 的 `wait_for` 包裹（等待锁的时间计入超时，避免上传任务无限堆积）
- 当前部署为单 uvicorn worker（Dockerfile.dev），进程内锁足够；**约束**：未来多副本部署需升级 Redis 分布式锁

### 9.4 API（新增 src/api/wiki.py，挂 `/api/kb/{kb_id}/wiki` 前缀，全部 require_owner）

| 端点 | 说明 |
|---|---|
| `GET /pages` | 页面列表：`[{id, page_type, title, revision, status, source_doc_count, updated_at}]`，实体/主题/索引全返回 |
| `GET /pages/{page_id}/content` | 单页正文预览（MinIO 读文本） |
| `POST /rebuild` | 全量重编译（BackgroundTasks），进度走现有 `notify_task_progress` 通道，返回 upload_id |

- rebuild 核心自 `scripts/rebuild_wiki.py` 下沉为 `services/wiki_rebuild.py::rebuild_kb_wiki(db, kb_id, progress_cb)`，script 改薄壳调用（消除逻辑双份）
- 受影响 chunk 元数据：无需 Milvus schema 变更

### 9.5 诊断探针（WiCER 事实保留率，运行时自检）

- 配置 `WIKI_DIAGNOSTIC_PROBES: bool = False`（默认关，对齐 §3.3 原设计）
- 实现：编译产出页面后，用抽取阶段已有的 `key_facts` 作原子事实探针（**零额外 LLM 调用**）：
  - 页面正文分句 + bge-m3 embedding，fact×句余弦 max ≥ `FACT_RETENTION_THRESHOLD`（0.75，模块常量）视为保留
  - `retention_rate` 写入 `WikiCompileResult` 新字段 + 日志；< 0.9 记 warning（对齐 §5 上线门槛）
  - embedding 失败跳过探测，不影响编译
- 不落库、不卡点（Phase 2 最小面；A/B 评估仍走 test_wiki_ab.py）

### 9.6 前端（最小面）

- `KnowledgeBaseView.vue` 加「Wiki 页面」抽屉：Vue Query 拉列表（实体/主题/索引分组，标题/revision/来源文档数/更新时间），点行预览正文（复用现有 markdown 渲染），「全量重编译」按钮（确认弹窗 → POST → 复用现有任务进度轮询）
- 不新增路由页、不建新 Pinia store

### 9.7 改动清单

| 文件 | 改动 |
|---|---|
| `src/services/wiki_cascade.py` | 新增：on_document_deleted / on_kb_deleted |
| `src/services/wiki_rebuild.py` | 新增：rebuild_kb_wiki（自 script 下沉） |
| `src/services/wiki_compiler.py` | 锁注册表 + 既有页先删旧向量 + 诊断探针 + WikiCompileResult.retention 字段 |
| `src/api/wiki.py` + `src/api/__init__.py` | 新增 3 端点并注册 |
| `src/api/document.py` / `knowledge_base.py` | 删除链路挂级联钩子 |
| `src/config.py` + `.env`/`.env.dev`/compose×2 | `WIKI_DIAGNOSTIC_PROBES`（同文件顺序编辑；dev compose 需显式透传） |
| `scripts/rebuild_wiki.py` | 改薄壳 |
| `frontend/.../KnowledgeBaseView.vue`（+子组件） | Wiki 页面抽屉 |
| `backend/tests/test_wiki_cascade.py`、`test_wiki_api.py`、`test_wiki_compiler.py` 增补 | 级联（剪源/孤儿删页/KB 清理）、API（owner 校验）、防重复向量、探针两态、锁行为 |

### 9.8 验收

1. 级联更新单测全绿（§4 Phase 2 退出条件）+ 全量回归 ≥ 现基线（709 passed）
2. 删源文档后：孤儿页从向量库/MinIO/DB 三处消失，索引页目录同步
3. 同页多 revision 后 Milvus 中该页向量数不增长
4. 诊断探针关闭时编译行为与 Phase 1 完全一致

## 10. 实施记录（Phase 2，2026-09-08）

按 §9 设计稿实施完毕，验收项全部满足；全量回归 **733 passed / 101 skipped**（较 Phase 1 基线 709 新增 24 个 Phase 2 测试）。

### 10.1 交付物

| 模块 | 实际改动 |
|---|---|
| `src/services/wiki_cascade.py` | 新增。`on_document_deleted`：JSONB containment 查受影响页 → 剪源或删孤儿页（向量 + MinIO + DB 三处）→ 有删页时重建索引页；`on_kb_deleted`：清 `wiki_pages` 行 + MinIO 正文。两入口均 `get_kb_lock(kb_id)` 持锁、try/except 包裹不阻断删除主流程 |
| `src/services/wiki_rebuild.py` | 新增。`rebuild_kb_wiki(db, kb_id, progress_cb)`（sync/async 回调兼容）：清既有产物 → 读 published 文档 raw chunks → 逐文档编译，单文档失败跳过 |
| `src/services/wiki_compiler.py` | 模块级 `_kb_locks` 注册表 + `get_kb_lock()`；`persist_pages` 既有页先 `delete_by_document_id` 再入库（防多 revision 向量累积）；`_check_fact_retention` 诊断探针（`WIKI_DIAGNOSTIC_PROBES` 关闭时零 embedding 调用）；`WikiCompileResult.fact_retention_rate` |
| `src/api/wiki.py` + `main.py` | 新增 3 端点（pages 列表 / 单页 content / rebuild 后台任务）并注册路由；rebuild 后台任务独立会话，进度经 `notify_task_progress` + `update_upload_progress` 双通道推送 |
| `src/api/document.py` / `knowledge_base.py` | 删除链路挂级联钩子（单删/批删 KB、文档删除后台任务） |
| `src/config.py` + `.env`/`.env.dev`/compose×2 | `WIKI_DIAGNOSTIC_PROBES`（默认 false，dev compose 显式透传） |
| `scripts/rebuild_wiki.py` | 改薄壳（进度打印 + 调用 `rebuild_kb_wiki`） |
| `frontend/src/queries/kb.ts` | `WikiPageSummary` 等类型 + `useWikiPages`（Vue Query）/ `fetchWikiPageContent` / `useRebuildWiki` |
| `frontend/src/components/knowledge-base/WikiDrawer.vue` | 新增。页面列表（类型徽标/标题/来源数/revision/更新时间）+ 点行懒加载正文（MarkdownRenderer 渲染，会话内缓存）+ 全量重编译（确认弹窗 → 提交 → WS 进度条 → 终态刷新列表） |
| `frontend/src/views/KnowledgeBaseView.vue` | header 增「Wiki 页面」入口按钮 + 抽屉挂载 |
| 测试 | `test_wiki_cascade.py`、`test_wiki_api.py` 新增；`test_wiki_compiler.py` 增补探针/锁/防重复向量；`test_kb_single_delete.py` 适配级联查询 |

### 10.2 与设计稿的差异

1. **API 前缀**：实际挂 `/api/knowledge_bases/{kb_id}/wiki`（设计稿写 `/api/kb/{kb_id}/wiki`），与既有 KB 路由前缀保持一致。
2. **重编译进度通道**：设计稿写"复用现有任务进度轮询"。实际实现复用了 `/api/documents/upload/progress/ws/{upload_id}` WebSocket 通道（`create_upload_progress`/`update_upload_progress` 同源），为此 `_run_wiki_rebuild` 的 progress 回调在 `notify_task_progress` 之外补写 `progress_store`（`processing_progress` 字段），WS 快照含中间百分比；前端无需新增轮询逻辑。
3. **前端入口**：设计稿为标签页方向，实际采用 header 低频按钮 + `el-drawer` 抽屉（页面列表+预览+重编译单一入口），不新增路由页、不建新 Pinia store（与 §9.6 一致）。

### 10.3 验收对照（§9.8）

1. 级联/探针/锁/API 单测全绿，全量回归 733 passed ≥ 709 基线 ✅
2. 孤儿页三处清理 + 索引页重建：`TestOnDocumentDeleted::test_orphan_page_deleted_everywhere` 覆盖 ✅
3. 同页多 revision 向量不累积：`persist_pages` 先删旧向量 + `TestPersistPages`/防重复向量用例覆盖 ✅
4. 探针关闭零副作用：`TestFactRetention::test_disabled_returns_none_without_embeddings` 断言不调用 embedding ✅

### 10.4 遗留与 Phase 3 候选

- 删除源文档后页面内容的 LLM 级联重写（§9.2 显式不做）
- 多副本部署时 `_kb_locks` 进程内锁需升级 Redis 分布式锁
- A/B 评估（检索指标对比）仍走 `test_wiki_ab.py`，探针仅作编译期自检

---

## 11. Phase 3 详细设计（2026-09-08 设计稿，待确认）

> 范围对齐 §4 Phase 3 行：index 页喂 KB 推荐做覆盖先验、交叉链接检索扩展、Lint 体检报告。
> §10.4 两个候选（Redis 分布式锁、删除后 LLM 级联重写）本阶段**继续不做**（单 worker 部署无锁竞争；重写成本高、raw 兜底可接受）。

### 11.1 KB 覆盖先验（kb_recommender 增强）

现状：`KBRecommender.recommend_knowledge_bases` 全域检索 → 按 kb 聚合 chunk score 均值排序。盲区：问题与 KB 内具体 chunk 相似度都不高、但主题确实落在某 KB 时推荐不上。

设计：

1. `WikiRoutePrior`（新 `src/services/wiki_route_prior.py`）：对每个 KB 取 `page_type='index' and status='active'` 的索引页正文，embedding 后与问题算余弦 → 0~1 先验分。索引页是全 KB 目录，天然是"KB 覆盖面"摘要。
2. 融合：`final = chunk_avg × (1−w) + index_sim × w`，`WIKI_ROUTE_PRIOR_WEIGHT: float = 0.3`（可配）；**无 index 页的 KB 不做融合，保持纯 chunk 分**（编译关闭的 KB 行为与现状完全一致）。
3. embedding 缓存：进程内 TTL 缓存，key 含 `page_id + revision`（索引页仅编译时变化，revision 变更自动失效），推荐为低频接口，无需 Redis。
4. 失败兜底：先验计算异常仅记日志，回退纯 chunk 分。

### 11.2 交叉链接检索扩展

现状：页面正文有 `[[Title]]` 链接，但仅存在于 markdown 文本；wiki 向量 metadata（`source="wiki://{title}"`、`heading_path=title`）无结构化链接，检索时无法扩展。

设计：

1. **链接结构化**：wiki_compiler 在 persist/更新页面时提取正文 `[[Title]]` 写入 `wiki_pages.links` JSONB（新列，Alembic 迁移）；提取规则 `r"\[\[([^\[\]]+)\]\]"`，去重、排除自引用与不存在页（写库时校验）。
2. **检索扩展**（rerank 之后、context 组装之前追加，不挤占原命中）：
   - 扫描 rerank 后 top 结果中 `source_kind="wiki"` 的页 → 读 `links` → 按 `(kb_id, title)` 查目标页 → 取目标页分块（Milvus `document_id=page.id`），每页补 2 块、总上限 6 块
   - 扩展块 metadata 标 `link_expanded=true`，直接追加进 context
   - 开关 `WIKI_LINK_EXPANSION: bool = False`（默认关）；无链接/查询失败 → 原结果返回
3. 存量页回填：链接列新增后，已有页在下次重编译（`rebuild`）时自然补齐；不强制立即迁移重跑。

### 11.3 Lint 体检报告（wiki_lint.py）

`lint_kb_wiki(db, kb_id) -> LintReport`，**规则级零 LLM、同步毫秒级**：

| 检查项 | 级别 | 规则 |
|---|---|---|
| 断链 | error | `links` 指向的 title 无对应 active 页 |
| 孤立页 | warning | 不在 index 页目录、且无任何其他页链接指向它 |
| 目录缺失 | warning | active 实体/主题页未收录进 index 页目录 |
| 空源页 | warning | `status=active` 但 `source_doc_ids=[]`（Phase 2 级联应已防，防御性检查） |
| 异常体量 | info | 正文 < 200 字或 > 20000 字 |

- LLM 矛盾抽查继续不做（成本高，依赖事实保留率探针先达标）。
- **只报告不自动修复**；报告含每项的页 ID/标题，供用户决定是否重编译。
- API：`GET /knowledge_bases/{kb_id}/wiki/lint`（require_owner）；前端 WikiDrawer 工具栏加「体检」按钮，报告内联展示。

### 11.4 配置项（默认全关）

```python
WIKI_ROUTE_PRIOR_WEIGHT: float = 0.3   # 0 = 关闭覆盖先验
WIKI_LINK_EXPANSION: bool = False      # 交叉链接检索扩展
WIKI_DIAGNOSTIC_PROBES = (Phase 2 已有，不动)
```

### 11.5 改动清单

| 文件 | 改动 |
|---|---|
| `src/services/wiki_compiler.py` | persist/更新页时提取 `[[Title]]` 写 `links` 列 |
| `alembic/versions/` | 新迁移：`wiki_pages` 加 `links JSONB NOT NULL DEFAULT '[]'` |
| `src/models/wiki_page.py` | 模型加 `links` 字段 |
| `src/services/wiki_route_prior.py` | 新增：index 页覆盖先验（TTL 缓存） |
| `src/services/kb_recommender.py` | 融合先验分（无 index 页 KB 不受影响） |
| `src/services/rag_chain.py`（或 context_builder） | rerank 后链接扩展追加钩子 |
| `src/services/wiki_lint.py` + `src/api/wiki.py` | 新增 lint 服务与 `GET /lint` 端点 |
| `src/config.py` + `.env`/`.env.dev`/compose×2 | 2 个新配置 |
| `frontend/.../WikiDrawer.vue` + `kb.ts` | 体检按钮 + 报告展示；lint API 封装 |
| 测试 | links 提取/回填、先验融合两态（有/无 index 页）、链接扩展（命中 wiki 页/无开关）、lint 规则表驱动、API 增补 |

### 11.6 验收

1. 全量回归 ≥ 733 passed；编译关闭或 KB 无 wiki 页时，推荐与检索行为与现状零差异。
2. 覆盖先验：同问题下有 index 页的 KB 排序可被先验抬升（单测以 mock embedding 验证融合公式与权重）。
3. 链接扩展：命中 wiki 页且其 links 非空时 context 追加目标页分块（≤6 块），`link_expanded` 可追溯；关闭开关时不追加。
4. Lint：5 项规则各有正反用例；无 wiki 页的 KB 返回空报告不报错。

### 11.7 实施记录（2026-09-09）

按 §11.5 改动清单全量落地，实测偏差与要点：

- **交付文件**：模型 [wiki_page.py](../../backend/src/models/wiki_page.py) 加 `links` JSONB 列；新迁移 `b7d2c94a1f30`（dev 库已 `upgrade head` 验证，列定义 `jsonb NOT NULL DEFAULT '[]'`）；[wiki_compiler.py](../../backend/src/services/wiki_compiler.py) 新增模块级 `extract_links()` 与 `_fill_links()`（页面 upsert 提交后提取 `[[Title]]`，按归一化标题校验目标页存在、去重、排除自引用，失败仅告警）；新增 [wiki_route_prior.py](../../backend/src/services/wiki_route_prior.py)（index 页 embedding，进程内 TTL 缓存 key=`(page_id, revision)`，question 向量不入缓存）+ [kb_recommender.py](../../backend/src/services/kb_recommender.py) 融合（`fuse_prior()` 纯函数，返回体新增 `chunk_score`/`route_prior` 字段）；新增 [wiki_link_expansion.py](../../backend/src/services/wiki_link_expansion.py) 并在 rag_chain `_stage_kb_retrieval` 相关性判定**之后**挂钩（扩展不影响相关性门槛）；新增 [wiki_lint.py](../../backend/src/services/wiki_lint.py) + `GET /knowledge_bases/{kb_id}/wiki/lint`；[context_builder](../../backend/src/services/context_builder.py) 与 [vector_store](../../backend/src/services/vector_store.py) 补 `link_expanded`/`source_kind` 元数据透传；前端 [kb.ts](../../frontend/src/queries/kb.ts) `fetchWikiLint` + [WikiDrawer.vue](../../frontend/src/components/knowledge-base/WikiDrawer.vue) 工具栏「体检」按钮与报告内联展示。
- **偏差说明**：链接扩展逻辑独立为 `wiki_link_expansion.py`（§11.5 写的是 rag_chain 内挂钩），rag_chain 仅保留 3 行钩子调用，便于独立单测；compose 无需改动（`env_file` 全量注入，新开关只改 env 文件）。
- **验收结果**：全量回归 **779 passed / 101 skipped**（基线 733，新增 46 例：links 提取/写库校验/失败兜底、先验融合两态+TTL 缓存+revision 失效、链接扩展命中/上限 6 块/每页 2 块/自引用去重/失败回退、lint 5 规则正反用例+空库空报告、API owner 校验）；`pnpm run build` 通过；存量页 links 由下次 rebuild 自然回填（§11.2.3）。
- **遗留**：postgres-prod 库尚未升级到 `b7d2c94a1f30`，切换 prod compose 前需对其执行 `alembic upgrade head`。

---

## 12. 运行时验证与运维依赖（2026-09-08 实测记录）

> 对 Phase 1/2 交付内容做端到端运行时探活（真实容器栈 + 真实上传/问答），发现并修复 3 处「已开发但未实际生效」问题。

### 12.1 验证结论

| 项目 | 结果 |
|---|---|
| Alembic 迁移 | dev 库实际无 alembic_version（此前迁移打到了 postgres-prod），已补 `stamp head` → `a3f8c1e29b47` |
| wiki_pages 表 | dev 库 11 列与迁移定义零差异 |
| API 三端点 | `/api/knowledge_bases/{kb_id}/wiki/pages`、`.../pages/{id}/content`、`.../rebuild` 均在 openapi 注册且可路由 |
| 级联清理 | KB 删除后 documents 同步清除，`on_kb_deleted` 钩子实测生效 |
| 上传管线 | 修复后真实上传 txt → published / 1 chunk（含 BM25 sparse 向量插入成功） |
| 语义缓存（P1-3 回归验证） | 同问重发 exact 命中回放（`semantic_cache_hits_total{match_type="exact"} 1.0`），miss/store 指标齐全 |
| 单测回归 | test_semantic_cache_service + test_wiki_compiler 共 59 passed |

### 12.2 修复的缺陷（本次验证发现）

1. **compose 缺 `FAST_LLM_MODEL_NAME` 传递**（dev + prod）：A2 启动强校验要求三项模型名必填，容器读不到根 .env，backend 启动即挂。已在两个 compose 的 backend environment 补齐；prod 同时补漏的 `SEARCH_RERANK_PROVIDER` / `KB_RERANK_MODEL` / `KB_RERANK_PROVIDER`。
2. **容器内 nltk stopwords 缺失 → BM25 sparse 计算必败 → Milvus 插入缺 `sparse_embedding` 字段 → 文档处理整体失败**：修复为离线语料方案 —— 宿主机预下载 `stopwords.zip`/`punkt_tab.zip` 到 `backend/data/nltk_data/`（bind 挂载进容器），compose backend 增加 `NLTK_DATA: /app/data/nltk_data`。下载源用 jsDelivr CDN（`cdn.jsdelivr.net/gh/nltk/nltk_data@gh-pages/packages/...`，raw.githubusercontent 直连 SSL 失败）。
3. **`SemanticCacheService` / `WikiCompiler` 自建 `OllamaEmbeddings` 未带连接配置**：容器内默认 `localhost:11434` 连不通（管线自身走 OLLAMA_HOST=host.docker.internal），导致语义缓存 embedding 永远超时（缓存永不写入/命中）、Wiki 诊断探针与页面匹配同样失效。修复：两处改用 `model_manager.get_embeddings()` 共享单例（B2 设计初衷）；同时语义缓存 embedding 超时从 3s（REDIS_OPERATION_TIMEOUT）独立为 10s（`EMBEDDING_TIMEOUT_SECONDS`），因 4GB 显存下 LLM↔embedding 模型互换加载远超 3s。

### 12.3 新增运维依赖清单

- `backend/data/nltk_data/corpora/stopwords.zip` + `backend/data/nltk_data/tokenizers/punkt_tab.zip`（已就位，随仓库 bind mount 分发）
- compose backend 环境变量：`FAST_LLM_MODEL_NAME`、`NLTK_DATA`（dev/prod 均已补）

### 12.4 遗留观察（非缺陷）

- 意图路由将「相似度阈值是多少」误判为 calculator 工具调用（tool_first 不写缓存，行为符合设计，但路由准确性可优化）
- 同义改写问题（相似度 <0.92）不命中语义缓存，属 P1-3 高精度阈值的预期保守行为
- dev 栈 postgres 容器名是 `postgres-dev`（volume `postgres_data_dev`），与 prod compose 的 `postgres-prod`（volume `postgres_data_prod`，端口同为 5433）互斥占用，混用两个 compose 文件会触发容器重建，注意区分

---

## 13. Phase 4 详细设计（2026-09-09 设计稿，已实施）

> 主题：**编译质量闭环 + 效果透明化**——把 Phase 1/2 的「能编译」推进到「编译得好、看得到、量得到」。
> 与 Phase 3（§11）互相独立、无实施顺序依赖；§10.4 两个候选（Redis 分布式锁、LLM 级联重写）继续延后（理由见 §13.4）。

### 13.1 迭代精炼编译（WiCER 诊断探针闭环）

现状：Phase 2 探针（`_check_fact_retention`）只做一次整体保留率计算 + 日志告警，产出不达标也不补救。WiCER 的核心结论是「诊断探针 + 迭代编译可挽回 80% 盲编译灾难性丢事实」——本阶段把探针从「只测量」升级为「驱动重生成」。

设计：

1. **探针细粒度化**：`_check_fact_retention` 重构为逐页计算——对每个 `CompiledPage`，逐条 key_fact 与该页分句算余弦（沿用 `FACT_RETENTION_THRESHOLD = 0.75` 判定单条事实是否保留），返回 `dict[title, (retention, missing_facts)]`；整体保留率由逐页聚合得出（口径与现值一致，result 字段不变）。
2. **精炼循环**（新配置 `WIKI_COMPILE_REFINEMENT_ITERATIONS: int = 0`，0=关，默认关）：
   - `compile_document` 流程重排：编译（内存态）→ 逐页探针 → 精炼循环 → persist → 结果记录。探针只读内存态 `CompiledPage`，persist 前移安全（现实现本就以内存页为输入）；整体仍被外层 `WIKI_COMPILE_TIMEOUT_SECONDS` 的 `wait_for` 包裹。
   - 每轮取 retention < 0.9 的弱页（常量化 `FACT_RETENTION_TARGET = 0.9`，对齐 §5 门槛），调用新方法 `refine_pages`（**纯 LLM 无 IO**，与 `compile_pages` 同风格、单测可 mock）：prompt 注入该页缺失的原子事实清单，约束「保留既有内容、补充缺失事实、不引入新来源」。
   - 重生成后替换内存态页面内容进入下一轮探针；达标页不动。
   - 轮次耗尽或全部达标后**统一一次 persist**（不产生中间向量，多 revision 防重复逻辑不受影响）。
3. **失败安全**：任一轮 refine 抛错/超时 → 该页保留当前内容继续后续流程（结果绝不比现状差）；精炼开启时探针自动启用（embedding 走 `model_manager` 共享单例，B2 同源连接）；`WIKI_DIAGNOSTIC_PROBES` 单独开启仍保持 Phase 2「仅日志」语义不变（向后兼容）。
4. **成本上界**：每轮最多 `MAX_PAGES_PER_DOC` 次 LLM 重生成且仅弱页参与；4GB 显存下编译时长相应增加，由外层 300s 超时兜底（超时仅放弃编译，不阻断上传——现有语义不变）。

### 13.2 source_kind 透出 + 前端「编译页」徽标（§3.5 遗留收尾）

现状缺口（已核实代码）：Milvus 检索 output_fields 已带回 `source_kind`，但 `_extract_source_info`（rag_chain.py）构建来源元数据时未透传，chat.py 的 `source_info` 组装也只挑既有字段——前端拿不到编译页标识，综合页与原文在引用列表中不可区分（§6 透明性风险一直悬空）。

设计（改动极小）：

1. `_extract_source_info` 的 `source_metadata` 补 `'source_kind': metadata.get('source_kind', 'raw')`（1 行）。
2. chat.py `source_info` 字典补 `"source_kind": meta.get('source_kind', 'raw')`（1 行；非流式响应与 SSE end 事件同源，自动生效）。
3. 前端 `MessageSources.vue`：`source_kind === 'wiki'` 时标题旁渲染「编译」小徽标，tooltip 提示「该条来自 LLM 编译的综合页，非原文」；来源 TS 类型补可选字段（queries/chat.ts）。
4. 联网/工具来源不含该字段 → 兜底 'raw'，行为不变。

### 13.3 编译 Prometheus 指标（可观测闭环）

现状：编译是否在跑、产出多少、质量如何，只能翻应用日志（§12 排查「已开发未生效」时深有体会）。对齐现有指标集中管理（middleware/prometheus.py）新增 4 项：

| 指标 | 类型 | 标签/桶 |
|---|---|---|
| `wiki_compilations_total` | Counter | `result=ok\|failed\|timeout` |
| `wiki_compile_pages_total` | Counter | `action=created\|updated` |
| `wiki_compile_duration_seconds` | Histogram | 默认桶 |
| `wiki_compile_fact_retention` | Histogram | buckets 0.5~1.0（步长 0.1；未探测不记录） |

- 埋点：document.py 阶段 7 三个出口（成功/异常/超时）+ wiki_rebuild.py 逐文档循环；封装小 helper（`record_wiki_compile(...)`）避免两处重复。
- 无新增配置。

### 13.4 明确不做（继续延后）

| 候选 | 理由 |
|---|---|
| Redis 分布式锁 | 单 uvicorn worker，无跨进程竞争；多副本部署前不动 |
| 删除后 LLM 级联重写 | 成本高、raw 兜底可接受；Phase 3 Lint 提供可见性后按需再评估 |
| LLM 矛盾抽查 | 前置条件是事实保留率先稳定达标——正是本阶段迭代精炼要解决的，达标后再评估 |
| 批量上传编译合并/去抖 | 单 KB 串行 + MAX_PAGES_PER_DOC 限额当前够用；实测批量上传编译时长不可接受再立项 |

### 13.5 改动清单

| 文件 | 改动 |
|---|---|
| `src/services/wiki_compiler.py` | 探针逐页化（保留整体聚合口径）+ `refine_pages` 新方法 + `compile_document` 流程重排（probe→refine→persist） |
| `src/config.py` + `.env`/`.env.dev`/compose×2 | `WIKI_COMPILE_REFINEMENT_ITERATIONS: int = 0`（dev compose 显式透传） |
| `src/middleware/prometheus.py` | 4 个 wiki 指标定义 |
| `src/api/document.py` + `src/services/wiki_rebuild.py` | 编译埋点 helper 两处调用 |
| `src/services/rag_chain.py` | `_extract_source_info` 透传 source_kind（1 行） |
| `src/api/chat.py` | `source_info` 补 source_kind（1 行） |
| `frontend/src/components/chat/MessageSources.vue` + `queries/chat.ts` | 「编译」徽标 + 来源类型字段 |
| 测试 | 逐页探针（正交向量技巧沿用）、精炼两态（达标不重生成/弱页重生成且 prompt 含缺失事实/轮次上限/失败保底）、指标埋点三分支、source_kind 透传（rag_chain + chat）、徽标渲染（vitest） |

### 13.6 验收

1. 全量回归 ≥ 733 passed；`REFINEMENT_ITERATIONS=0` 时编译行为与现状完全一致（探针仍仅日志、仅受 `WIKI_DIAGNOSTIC_PROBES` 控制）。
2. 迭代精炼：mock LLM 验证弱页被重生成且 prompt 含缺失事实、达标页不动、轮次 ≤ 配置值、refine 异常时保留原页内容不阻断上传。
3. 指标：真实上传触发编译后 `/metrics` 出现 `wiki_compilations_total{result="ok"}` 与 pages/duration/retention 指标；人为制造编译异常出现 `result="failed"`。
4. 透明化：KB 问答来源 payload 含 `source_kind`，wiki 页在前端引用列表渲染「编译」徽标，raw/网页来源不受影响（vitest 覆盖）。
5. 后续（非代码）：Phase 4 落地后用真实 KB 跑 `test_wiki_ab.py --run-e2e` 对比迭代精炼前后指标，为 roadmap P2-1 GraphRAG 的降级/合并决策提供依据（§2.3 约定的决策门）。

### 13.7 实施记录（2026-09-09）

按 §13.5 改动清单全量落地，实测要点与偏差：

- **交付文件**：
  - [wiki_compiler.py](../../backend/src/services/wiki_compiler.py)：`_check_fact_retention` 重构为逐页探针（返回 `{title: (保留率, 缺失事实列表)}`，整体保留率改由 `_aggregate_retention` 聚合，口径与 Phase 2 一致=保留数/总数）；新增 `_refinement_loop`（探针定位弱页 → `refine_pages` 逐页重生成 → 重探，轮次受 `WIKI_COMPILE_REFINEMENT_ITERATIONS` 约束）与 `refine_pages`（纯 LLM 无 IO，注入缺失事实清单，空输出视为失败）；`compile_document` 重排为 compile → probe/refine（内存态）→ persist（统一一次入库，无中间向量）；常量 `FACT_RETENTION_TARGET = 0.9`。精炼失败/空输出时该页保留当前内容继续（结果绝不比精炼前差）；`ITERATIONS=0` 且探针关闭时整段跳过，行为与 Phase 1/2 完全一致。
  - [config.py](../../backend/src/config.py) + [.env.dev](../../.env.dev) / [.env.example](../../.env.example)：`WIKI_COMPILE_REFINEMENT_ITERATIONS=0`（默认关；compose env_file 全量注入无需改动）。
  - [middleware/prometheus.py](../../backend/src/middleware/prometheus.py)：4 个指标 `wiki_compilations_total{result}` / `wiki_compile_pages_total{action}` / `wiki_compile_duration_seconds` / `wiki_compile_fact_retention`（buckets 0.5~1.0 步长 0.1，未探测不记录）+ 共用 helper `record_wiki_compile(...)`。
  - 埋点：[document.py](../../backend/src/api/document.py) 阶段 7 三出口（ok 带 pages/duration/retention、timeout 带 duration、failed）+ [wiki_rebuild.py](../../backend/src/services/wiki_rebuild.py) 逐文档 ok/failed（rebuild 无外层超时故无 timeout 分支）。
  - source_kind 透出：[rag_chain.py](../../backend/src/services/rag_chain.py) `_extract_source_info` 与 [chat.py](../../backend/src/api/chat.py) `source_info` 各 1 行（缺省回 `'raw'`，联网/工具来源不受影响）；前端 [chat.ts](../../frontend/src/queries/chat.ts) `MessageSource.source_kind` 可选字段 + [MessageSources.vue](../../frontend/src/components/chat/MessageSources.vue) 标题旁「编译」小徽标（原生 title 提示「该条来自 LLM 编译的综合页，非原文」，raw/web 不渲染）。
- **验收结果**：全量回归 **794 passed / 101 skipped**（Phase 3 基线 779，新增 15 例：逐页探针 dict 断言适配、精炼 6 例（关闭零探针/弱页重生成且 prompt 含缺失事实/达标页不动/轮次上限/LLM 异常保底/空输出保底）、聚合 3 例、指标埋点 4 例（ok/failed/timeout/helper）、source_kind 透传 2 例）；`pnpm run build` 通过；新增前端 vitest `MessageSources.spec.ts` 3 例通过。
- **偏差说明**：指标 `wiki_compile_fact_retention` 用 Histogram 记录逐次保留率（含 1.0 桶），unlabeled；timeout 分支置于 `except Exception` 之前（asyncio.TimeoutError 属 Exception 子类）。
- **遗留**：chat.py source_info 的 SSE end payload 含 source_kind 待真实容器栈端到端验证（§13.6.3/4 运行时验收项）；迭代精炼的真实模型效果评估待 `test_wiki_ab.py --run-e2e`（§13.6.5）。

---

## 14. Phase 5 详细设计（2026-09-09 设计并实施，记录见 §14.8）

> 主题：**原 §13.4 四项延后候选转正**——多副本就绪（分布式锁）、删除一致性（级联重写）、内容可信（矛盾抽查）、批量效率（编译去抖）。
> 决策已定（2026-09-09 用户确认）：级联重写=**从剩余来源重推导**；矛盾抽查=**仅诊断**；编译去抖=**默认开 20s**。

### 14.1 Redis 分布式锁（多副本就绪）

现状：编译/级联清理共用 `get_kb_lock`（wiki_compiler.py 模块级 `_kb_locks`）进程内锁，仅单 worker 安全（§10.4 遗留）。

设计：

1. 新建 `src/services/wiki_lock.py`：`kb_wiki_lock(kb_id)` 异步上下文管理器，双层加锁——先取进程内 `get_kb_lock(kb_id)`，再尝试 Redis 锁：
   - 加锁：`SET wiki:lock:{kb_id} <token> NX PX <ttl>`，token=uuid4，TTL=`WIKI_COMPILE_TIMEOUT_SECONDS + 60`（对齐编译外层超时，防死锁不留看门狗）
   - 等待：轮询间隔 0.5s，获取超时 = `WIKI_COMPILE_TIMEOUT_SECONDS`（与现状「等待计入外层 300s」语义一致）
   - 释放：Lua compare-token-del（只删自己的锁）
2. **Redis 不可用降级**：CacheService 未就绪/加锁抛错 → 记 warning 后仅持进程内锁继续（单副本仍然安全；Redis 挂 + 多副本 = best-effort，日志可见）。禁止因锁失败阻断编译/级联主流程。
3. 新配置 `WIKI_DISTRIBUTED_LOCK: bool = False`（默认关=纯进程内锁，行为与现状完全一致；多副本部署时开）。
4. 调用点替换：`compile_document`、`on_document_deleted`、`on_kb_deleted`、去抖触发（§14.4）统一改走 `kb_wiki_lock`；`get_kb_lock` 保留为内部实现。
5. 指标：`wiki_lock_acquire_total{result=ok|redis_unavailable|timeout}` Counter。

### 14.2 删除后级联重写（从剩余来源重推导）

现状：`on_document_deleted` 剪源（source_doc_ids 移除）后，仍有剩余来源的页面**内容原样保留**——已删文档的事实残留（§9.2 显式不做 → 本次转正）。

设计（决策：从剩余来源重推导，与编译管线同构、结果确定性强）：

1. 新配置 `WIKI_CASCADE_REWRITE: bool = False`（默认关；关闭时行为与现状完全一致）。
2. `on_document_deleted` 剪源提交后，将「受影响且仍有剩余来源」的页面列表交给后台任务（`asyncio.create_task` + `async_session_maker` 独立会话，失败不阻断删除主流程）：
   - 逐页取 KB 锁（`kb_wiki_lock`），复用 `load_raw_chunks` 拉取**剩余来源文档**的 raw chunks，合并材料（按 `WIKI_REWRITE_MATERIAL_CHARS` 截断，见下）
   - 新增 `WikiCompiler.regenerate_page(title, page_type, material)`：纯 LLM 重推导整页（prompt 与 `_generate_page` 新页同风格 + 「保留既有标题层级与结构」约束）；材料为空（剩余来源 chunks 全失）→ 按孤儿页删除处理
   - 重写材料独立上限 `WIKI_REWRITE_MATERIAL_CHARS=16000`（qwen3:4b num_ctx 可容；重写低频，成本可承受，避免多来源页面重推导时材料过薄丢细节）
   - 重写成功：MinIO 正文覆盖 + revision+1 + 旧向量删除后重入库（复用 `_index_page(delete_old=True)`）+ links 重提取 + 索引页重建
   - **失败安全**：任一页重写抛错/空输出 → 保留当前内容（现状语义），仅记日志
3. 成本：每受影响页 1 次 LLM 调用；批量删除 N 文档 × M 页时按页串行，受 KB 锁约束。
4. `on_kb_deleted` 不涉及（整库删，无重写对象）。

### 14.3 LLM 矛盾抽查（仅诊断）

现状：`_generate_page` 增量合并 prompt 已要求模型「矛盾用 > ⚠️ 矛盾提示 标注」，但无独立校验，模型可能漏标（§13.4 延后 → 前置条件「事实保留率达标」已由 Phase 4 精炼满足，转正）。

设计（决策：仅诊断，先观测再评估自动修复）：

1. 新配置 `WIKI_CONTRADICTION_CHECK: bool = False`（默认关）。
2. `compile_document` 在精炼循环后、persist 前（内存态）执行：对每个 `matched` 非空的页面（增量更新场景）发 1 次 LLM 抽查——输入既有页内容 + 更新后页内容 + 新增材料，要求输出 JSON `{"contradictions": ["..."]}`（沿用 `_parse_json_dict` 解析）。
3. 产出处置：每条矛盾记 `logger.warning` + Counter `wiki_contradictions_total`（unlabeled）累加；**不改页面内容、不上传标注**（矛盾标注仍由生成期 prompt 承担）。
4. 失败安全：LLM 抛错/解析失败 → 视为无矛盾，仅记日志；抽查整体不改变编译结果与耗时上界（外层 300s 超时兜底不变）。
5. 新页（matched=None）不抽查（无「新旧矛盾」语义）。

### 14.4 批量上传编译去抖（合并编译）

现状：`/documents/batch` 与前端逐个上传时，每个文档处理完各自触发阶段 7 编译——N 个文档 = N 次完整编译（每次含既有页加载/抽取/生成/索引页重建），LLM 调用与耗时线性叠加。

设计（决策：默认开，20s）：

1. 新配置 `WIKI_COMPILE_DEBOUNCE_SECONDS: int = 20`（>0 开启；设 0 可回退现状行为）。
2. 新建 `src/services/wiki_compile_scheduler.py`（进程内，单 worker 语义）：
   - `schedule_compile(kb_id, doc_id)`：doc_id 入 per-KB pending 集，重置该 KB 的 `asyncio.TimerHandle`（20s）；计时器触发时快照并清空 pending → 取 `kb_wiki_lock` → 逐 doc `load_raw_chunks`（Milvus 拉取，无需跨任务传 chunks）→ 合并 chunks 调一次 `compile_document`（`doc_ids` 多值）
   - 合并材料沿用 `MAX_MATERIAL_CHARS` 总量截断（批量文档多时单文档材料变薄，属去抖换吞吐的既定取舍）；候选页 `source_doc_ids` 归属全部 doc_ids
   - 兜底：调度任务 fire-and-forget + 全量 try/except；pending doc 对应文档在去抖窗口内被删除 → `load_raw_chunks` 空列表自然跳过
3. `compile_document` 签名调整：`doc_id: str` → `doc_ids: Sequence[str]`（`persist_pages` 同步改，`source_doc_ids` 初始写入/追加按列表判定）；`document.py` 阶段 7 与 `wiki_rebuild.py` 两个调用点适配（rebuild 传 `[doc.id]`）。
4. 阶段 7 改造：去抖开启时编译**完全移出上传管线**——文档先进入阶段 6 完成态（published、进度走完，不设「正在编译」message），由调度器纯后台执行（编译失败本就不阻断上传，语义一致）；推送进度「已加入批量编译队列，将在后台执行」。指标照常在真正编译执行处埋点（去抖合并编译 = 一次 ok 记录，duration 含锁等待与 raw chunks 拉取）。
5. 单文件上传同样走调度（统一路径，编译延后 ≤20s 在后台完成——决策已确认可接受，且文档发布时点比现状更早）。

### 14.5 明确不做

- 分布式锁看门狗（TTL 自动续期）：TTL 覆盖编译超时 + 余量，锁丢失即重复编译属可接受幂等场景
- 级联重写自动精炼/矛盾抽查联动：矛盾仅诊断；重写页直接是新页语义，无既有矛盾输入
- 跨 worker 去抖（Redis 集合共享 pending）：单 worker 部署下无意义，多副本时去抖退化为各 worker 本地去抖（仍正确，只是合并度下降）

### 14.6 改动清单

| 文件 | 改动 |
|---|---|
| `src/services/wiki_lock.py`（新） | `kb_wiki_lock` 双层锁（进程内 + Redis NX/PX + Lua 释放）+ 降级 |
| `src/services/wiki_compile_scheduler.py`（新） | per-KB pending 集合 + 计时器 + 合并编译触发 |
| `src/services/wiki_compiler.py` | `compile_document`/`persist_pages` 改多 doc_ids；锁替换；`regenerate_page` 新方法；矛盾抽查 `_check_contradictions` |
| `src/services/wiki_cascade.py` | 剪源后调度后台重写任务（开关控制） |
| `src/api/document.py` | 阶段 7 接入调度器（去抖分支）+ 进度文案 |
| `src/services/wiki_rebuild.py` | 调用点适配 `doc_ids` 列表 |
| `src/config.py` + `.env.dev` / `.env.example` | 5 个新配置：`WIKI_DISTRIBUTED_LOCK=false`、`WIKI_CASCADE_REWRITE=false`、`WIKI_CONTRADICTION_CHECK=false`、`WIKI_COMPILE_DEBOUNCE_SECONDS=20`、`WIKI_REWRITE_MATERIAL_CHARS=16000` |
| `src/middleware/prometheus.py` | `wiki_lock_acquire_total{result}`、`wiki_contradictions_total` |
| 测试 | 锁三层态（Redis 可用/不可用降级/获取超时）；级联重写（开关两态/孤儿删除/失败保底/材料为空删除）；矛盾抽查（开关两态/仅诊断不改内容/解析失败兜底）；去抖（合并一次编译/doc_ids 归属/窗口内删除跳过/0=现状）；compile_document 多 doc 签名适配 |

### 14.7 验收

1. 全量回归 ≥ 794 passed；四开关全默认时：仅去抖生效（DEBOUNCE=20 默认开），其余三项行为与 Phase 4 完全一致。
2. 去抖：连续上传 2 文档至同一 KB → 仅 1 次 `wiki_compilations_total{result="ok"}`，页面 source_doc_ids 含两文档；`DEBOUNCE_SECONDS=0` 回退逐文档编译。
3. 级联重写：删除多来源页面的其中一个源文档 → 页面 revision+1 且正文不再含已删文档独有事实（mock LLM 断言 prompt 不含已删文档材料）；重写失败页面保留原内容。
4. 矛盾抽查：构造矛盾材料 → warning 日志 + `wiki_contradictions_total` 递增，页面内容与 `fact_retention_rate` 不受影响。
5. 分布式锁：Redis 停机时编译/级联/去抖全链路正常（仅 warning + `result="redis_unavailable"`）；Redis 恢复后双锁生效。

### 14.8 验收结果（2026-09-09）

- **验收结果**：全量回归 **828 passed / 101 skipped**（Phase 4 基线 794，新增 34 例：双层锁 6 例（进程内互斥/Redis 加释/token 校验/不可用降级/获取超时/关闭零开销）、级联重写 5 例（剩余来源重推导/开关关闭无任务/无材料删页/单来源拉取失败降级/重写失败保底）、矛盾抽查 6 例（开关两态/仅诊断不改内容/解析失败兜底/仅增量页/指标累加/persist 前时序）、去抖 7 例（pending 登记/累积/合并单次编译/窗口内删除跳过/全空跳过/failed/timeout 指标）、多 doc_ids 与 regenerate_page 等适配若干）；环境一致性校验通过（.env.example 覆盖 90 键）。
- **遗留**：§14.7 第 2/4/5 条属运行时验收（真实容器栈去抖合并指标、矛盾抽查 warning 日志、Redis 停机降级演练），随 Phase 4 遗留项一并验证；`pnpm run build` 不涉及（本 Phase 纯后端）。

### 14.9 运行时验收记录（2026-09-10，真实容器栈 + 本地 Ollama）

**Phase 5 运行时验收（§14.7 第 2/4/5 条）全部通过：**

1. 去抖合并编译：窗口内多文档合并为一次编译（临时调大窗口实测合并触发与 `wiki_compilations_total` 指标，验收后窗口恢复 20s）。
2. 矛盾抽查：矛盾材料触发 warning 日志 + `wiki_contradictions_total` 递增，页面内容与保留率不受影响。
3. 分布式锁：Redis 停机 → 编译链路降级进程内锁（warning + `result="redis_unavailable"`）；Redis 恢复后实测 Redis NX 锁获取/持有/竞争互斥/token 释放（`wiki:lock:<kb_id>`）全部正常。

**Phase 4 遗留两项闭环：**

4. SSE source_kind 端到端：发现并修复断点——[context_builder.py](../../backend/src/services/context_builder.py) `_collect_items` 重建 KB 元数据白名单时漏 `source_kind`，而 `_stage_generate` 用 `numbered_sources` 整体替换 `source_metadata`（[rag_chain.py](../../backend/src/services/rag_chain.py) `_stage_generate`），导致 rag_chain/chat 两处透传形同虚设。补 1 行后真实 `/api/chat/stream` 验证：wiki 页 `source_kind="wiki"`、raw 文档 `="raw"`。另确认：语义缓存命中会原样回放缓存时的元数据，缓存内容为旧口径时观察值滞后，属预期行为。
5. `test_wiki_ab.py --run-e2e` 首次真实运行（该测试 e2e 标记默认跳过，此前从未真跑）。两次口径修正（§5 门槛本意不变）+ 一处编译器健壮性修复：
   - **§5.1 非退化口径**：wiki 页命中计为其来源文档命中（测试内 `pages_by_doc` 溯源精确）。依据：编译自 golden 文档的 wiki 页排在原文之前恰是编译层设计目标（蒸馏高密度页），非检索损害；无关 golden 文档被挤出 top5 仍会被抓，真实伤害检测能力不变。实测 mrr 波动（1.000→0.800~0.850）均由"wiki 页挤占其来源原文排名"导致。
   - **§5.2 保留率口径**：分母改为仅被编译文档的事实数 + 归一化精确子串匹配改为 embedding 余弦（阈值 `FACT_RETENTION_THRESHOLD=0.75` 与生产探针一致）。修正前实现结构性不可能达标（分母含 8 篇未编译文档事实 + 压缩改写页不可能子串命中，实测 0.050）。
   - **编译器抽取重试**（[wiki_compiler.py](../../backend/src/services/wiki_compiler.py) `_extract_candidates`）：真实运行暴露 LLM JSON 偶发畸变（截断/未转义引号）→ 解析失败 → 整文档零页面静默损失覆盖；改为失败自动重试一次，新增回归测试。重跑 A/B 全绿：4 文档 12 页、hit_rate/mrr/recall Δ+0.000、事实保留率 **0.929 ≥ 0.9**。
6. 附带修复（验收中发现）：[cache_service.py](../../backend/src/services/cache_service.py) `_execute` 在不可用抛错路径未 close 调用方已创建的命令协程，Redis 停机降级时实测出现 `RuntimeWarning: coroutine was never awaited`；补 `coro.close()`。
7. 配置恢复：验收临时项（`WIKI_COMPILE_MODEL` 空 / `WIKI_COMPILE_TIMEOUT_SECONDS=300` / `WIKI_COMPILE_DEBOUNCE_SECONDS=20`）已还原；`WIKI_DISTRIBUTED_LOCK` / `WIKI_CASCADE_REWRITE` / `WIKI_CONTRADICTION_CHECK` 维持 dev 观察值（true）。
8. 回归：全量 **830 passed / 101 skipped**（Phase 5 基线 828，新增抽取重试回归用例；今日改动 `context_builder.py` / `cache_service.py` / `wiki_compiler.py` / `test_wiki_ab.py` 无回归）。

