#!/usr/bin/env bash
# SEC-13：从 backup.sh 产物恢复 PostgreSQL 与 MinIO。
# 用法：BACKUP_DIR=/backup PG_FILE=... MINIO_DIR=... ./scripts/restore.sh
set -euo pipefail

: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:=rag_db}"
: "${POSTGRES_USER:=postgres}"
: "${MINIO_ALIAS:=local}"
: "${MINIO_BUCKET:=documents}"

if [[ -z "${PG_FILE:-}" || -z "${MINIO_DIR:-}" ]]; then
  echo "必须指定 PG_FILE 与 MINIO_DIR（backup.sh 产物）" >&2
  exit 1
fi

echo "[1/2] 恢复 PostgreSQL: $PG_FILE"
gunzip -c "$PG_FILE" | PGPASSWORD="${POSTGRES_PASSWORD:-}" psql \
  -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB"

echo "[2/2] 恢复 MinIO: $MINIO_DIR"
mc mirror --overwrite "$MINIO_DIR" "$MINIO_ALIAS/$MINIO_BUCKET"

echo "恢复完成；Milvus 请使用 milvus-backup restore 或按 runbook 重建索引。"
