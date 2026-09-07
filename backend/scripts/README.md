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

## OCR 深度解析类（P0-2b，需先 `uv sync --group ocr-mineru` / `--group ocr-paddle`）

| 脚本 | 用途 |
|---|---|
| `download_ocr_models.py` | OCR 模型预热：生成样例扫描件跑一次两后端，首次联网下载模型 + 冒烟验证 |

## 运行方式

```bash
cd backend
uv run python scripts/<脚本名>.py
```
