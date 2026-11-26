# backend/scripts 脚本说明

本目录为**手动执行**的运维/迁移脚本，不属于应用启动流程。均为一次性或排障用途，执行前请确认目标数据库状态。

## 数据迁移类（migrate_*）

| 脚本 | 用途 |
|---|---|
| `migrate_knowledge_base.py` | 知识库表结构迁移 |
| `migrate_document_fields.py` | 文档表字段调整 |
| `migrate_embedding_model.py` | 切换 embedding 模型后的向量重建 |
| `migrate_hybrid_index.py` | 混合检索所需的稀疏向量（BM25）索引迁移 |
| `migrate_owner_id.py` | 数据补齐 owner_id（多用户预留字段） |
| `migrate_session_messages_jsonb.py` | 会话消息改 JSONB 存储 |
| `migrate_experiment_tables.py` | A/B 实验相关表创建 |
| `migrate_to_minio.py` | 本地文档文件迁移至 MinIO |

> 这些脚本只需在对应历史变更发生时执行一次；新部署环境按需使用。执行状态以实际数据库 schema 为准。

## 排障/手动验证类

（一次性排障脚本已清理；如需手动验证 SSE 流式接口，可直接用前端页面或 curl。）

## 评估类

| 脚本 | 用途 |
|---|---|
| `run_eval.py` | 检索质量离线评估（BM25 栈 + 评估语料），指标低于阈值非零退出；CI rag-eval job 使用 |
| `collect_section_scores.py` | 整节回填判据采集：逐题输出小节父块分数、各阈值下的上下文规模与答案覆盖率；样本 < 5 不给推荐阈值。带 `--title-gap 0.15` 即为子节标题闸门那一档，与不带的那次对比就是 A/B |

`eval_questions/title_gap_holdout.json` 是子节标题闸门的 **holdout 题集**（10 题，取自
`deployment.md` §1/§3/§4/§9、`architecture.md` §3、`backup-restore.md` §6，与切阈值用的 5 题
无重叠）。它的用途是避免「在同一批题上调参又验证」——改动闸门相关逻辑后，请用**没参与调参**
的题重跑，而不是回放这 10 题。`note` 字段说明每题预期跨/集中哪个子节，脚本会忽略它。

`collect_section_scores.py` 用于复验 `KB_SECTION_EXPAND_MIN_SECTION_SCORE` / `SKIP_SCORE`：
题目文件里每题给 `facts` 才会算覆盖率（`--answer`），否则只输出分数分布。每格默认跑 1 次，
同一份上下文的覆盖率实测可差 0.33（生成噪声），要比较接近的方案加 `--rounds 3` 取平均；
脚本会自动标注「上下文完全相同」的阈值组，并按 fact 命中率列出不稳定的点。
完整参数与口径见脚本头部说明，取舍背景见 `docs/guide/deployment.md` §7.13。

## OCR 深度解析类（P0-2b，需先 `uv sync --group ocr-mineru` / `--group ocr-paddle`）

| 脚本 | 用途 |
|---|---|
| `download_ocr_models.py` | OCR 模型预热：生成样例扫描件跑一次两后端，首次联网下载模型 + 冒烟验证 |

## 一致性巡检（只读）

| 脚本 | 用途 |
|---|---|
| `consistency_audit.py` | 比对 Milvus / MinIO / Redis（语义缓存）与 DB，报告孤儿与缺失；**不做任何写入/删除** |

三条链路各自独立采集，单侧失败不影响其它侧。退出码：`0` 一致、`1` 发现差异、`2` 巡检未完整执行（连不上或查询失败）。
判定逻辑在 `src/services/consistency_audit.py`（纯函数，有单测覆盖），脚本只负责参数、打印与 JSON 落盘。

```bash
cd backend
uv run python scripts/consistency_audit.py                       # 全量巡检
uv run python scripts/consistency_audit.py --json-out /tmp/a.json --sample 10
uv run python scripts/consistency_audit.py --skip-minio          # 只查向量库+缓存
```

常见问题类型（报告里的 `kind` 稳定，可直接做告警规则）：

- `orphan_vector_doc` / `vector_for_unpublished_doc` / `vector_kb_mismatch` / `vector_kb_blank` / `vector_kb_missing`
- `missing_vector_doc`（文档已发布却检索不到）
- `orphan_object`（MinIO 无主文件）/ `missing_object`（DB 引用但文件已丢）
- `stale_cache_entry` / `stale_cache_kb` / `expired_cache_entry` / `dangling_cache_index`

> 报告出来后的清理是破坏性操作：先按 `subject` 人工确认，再走既定的删除流程；本脚本故意不提供 `--fix`。
> Milvus 深分页有窗口上限，超出时报告会标 `truncated: true` 并在 stderr 提示，不会假装扫全了。

## 运行方式

```bash
cd backend
uv run python scripts/<脚本名>.py
```
