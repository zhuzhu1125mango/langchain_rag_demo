# LLM-Wiki 编译层设计（RAG 前置知识编译增强）

> 状态：待评审（遵循「设计先行 → 确认后实施」流程）
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
