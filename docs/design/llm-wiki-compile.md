# LLM-Wiki 编译层设计（RAG 前置知识编译增强）

> 状态：Phase 1 已实施（2026-09-07，记录见 §8）；Phase 2 已实施（2026-09-08，记录见 §10）；Phase 3 设计稿见 §11（待确认）；运行时验证与运维依赖记录见 §12（2026-09-08）
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
