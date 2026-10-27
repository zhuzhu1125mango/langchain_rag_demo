#!/usr/bin/env bash
# SEC-13：PostgreSQL + MinIO 备份（Milvus 使用 milvus-backup 单独执行，见 runbook）。
# 用法：BACKUP_DIR=/backup ./scripts/backup.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:=rag_db}"
: "${POSTGRES_USER:=postgres}"
: "${MINIO_ALIAS:=local}"
: "${MINIO_BUCKET:=documents}"

echo "[1/3] PostgreSQL dump -> $BACKUP_DIR/postgres_$STAMP.sql.gz"
PGPASSWORD="${POSTGRES_PASSWORD:-}" pg_dump \
  -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  --no-owner --no-privileges | gzip > "$BACKUP_DIR/postgres_$STAMP.sql.gz"

echo "[2/3] MinIO mirror -> $BACKUP_DIR/minio_$STAMP/"
mkdir -p "$BACKUP_DIR/minio_$STAMP"
mc mirror --overwrite "$MINIO_ALIAS/$MINIO_BUCKET" "$BACKUP_DIR/minio_$STAMP"

echo "[3/3] Milvus backup（如已配置 milvus-backup）"
if command -v milvus-backup >/dev/null 2>&1; then
  milvus-backup create -n "backup_$STAMP"
else
  echo "  未安装 milvus-backup，跳过；请按 docs/guide/backup-restore.md 手动快照。"
fi

echo "备份完成：$BACKUP_DIR（$STAMP）"
