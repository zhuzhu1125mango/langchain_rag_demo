# 备份与恢复 Runbook（SEC-13）

> 目标：季度恢复演练可执行；PG/MinIO 可自动备份，Milvus 使用官方 `milvus-backup`（或重建索引）。

## 1. 备份频率

| 组件 | 方式 | 建议频率 | 保留 |
|---|---|---|---|
| PostgreSQL | `scripts/backup.sh`（pg_dump + gzip） | 每日 | 30 天 |
| MinIO | `mc mirror` | 每日 | 30 天 |
| Milvus | `milvus-backup create` | 每周 + 变更前 | 8 周 |
| 配置/密钥 | 独立密钥管理系统 | 变更时 | 永久 |

## 2. 执行备份

```bash
export POSTGRES_HOST=localhost POSTGRES_PORT=5432 POSTGRES_DB=rag_db POSTGRES_USER=postgres
export MINIO_ALIAS=local MINIO_BUCKET=documents BACKUP_DIR=/backup
./scripts/backup.sh
```

Milvus（需安装 milvus-backup 并配置 `backup.yaml`）：

```bash
milvus-backup create -n "backup_$(date +%Y%m%d_%H%M%S)"
```

## 3. 恢复演练（每季度）

1. 在隔离环境启动空 PG/MinIO；
2. 执行 `./scripts/restore.sh`（指定 `PG_FILE` 与 `MINIO_DIR`）；
3. Milvus：`milvus-backup restore -n <backup_name>`；若无快照，按原文重新入库/重建索引；
4. 启动应用，确认 `/health/ready` 为 200；
5. 抽查 3 个知识库问答，检查引用来源与权限隔离；
6. 记录演练时间、恢复耗时、数据缺失与改进项。

## 4. 回滚

- 数据库：停止写入 → 恢复备份 → `alembic upgrade head` 对齐 schema；
- 向量索引：参考 [model-provider-registry-2026-10.md](../design/model-provider-registry-2026-10.md) §12.7 的备份集合与回滚流程；
- 恢复后必须失效语义缓存（重建/回滚会自动执行）。

## 5. 供应链

- `.github/dependabot.yml`：pip / npm / GitHub Actions 每周更新；
- `.github/workflows/security-scan.yml`：Trivy HIGH/CRITICAL 阻断 + SPDX SBOM artifact；
- 基础镜像 digest pin 需在发布窗口由运维执行（`docker pull` 后取 `RepoDigests` 写回 Dockerfile）。
