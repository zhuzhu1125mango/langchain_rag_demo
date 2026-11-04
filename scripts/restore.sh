#!/usr/bin/env bash
# SEC-13：从备份产物恢复 PostgreSQL 与 MinIO。
#
# 用法：
#   PG_FILE=/backup/postgres_<stamp>.sql.gz [MINIO_DIR=/backup/minio_<stamp>] ./scripts/restore.sh
#   PG_FILE=/backup/app_prod_<stamp>.dump   [MINIO_DIR=...]                        ./scripts/restore.sh
#
# 支持两种 PG 产物（自动识别，不需要调用方区分）：
#   - *.sql.gz     ：backup.sh 产出（pg_dump 纯文本 + gzip）→ gunzip | psql
#   - *.dump       ：pg_dump -Fc 产出（custom 格式，魔数 PGDMP）→ pg_restore
#
# 为什么必须两种都支持：2026-11-04 的恢复演练发现，仓库 backups/ 里 4 份备份全是
# 部署时手工 `pg_dump -Fc` 的 .dump，而本脚本当时只会 `gunzip | psql`——按 runbook
# 恢复会当场失败，且失败点是「gzip: not in gzip format」，很容易被误判成备份损坏。
set -euo pipefail

: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:=rag_db}"
: "${POSTGRES_USER:=postgres}"
: "${MINIO_ALIAS:=local}"
: "${MINIO_BUCKET:=documents}"
# TARGET_EMPTY=0 允许往非空库导入（默认拒绝：非空库导入会因对象已存在而中断）
: "${TARGET_EMPTY:=1}"

if [[ -z "${PG_FILE:-}" ]]; then
  echo "必须指定 PG_FILE（backup.sh 的 .sql.gz，或 pg_dump -Fc 的 .dump）" >&2
  exit 1
fi
if [[ ! -f "$PG_FILE" ]]; then
  echo "PG_FILE 不存在: $PG_FILE" >&2
  exit 1
fi

psql_run() {
  PGPASSWORD="${POSTGRES_PASSWORD:-}" psql \
    -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v ON_ERROR_STOP=1 "$@"
}

# 1) 目标库必须为空（runbook §4.1 第 2 步）：非空库直接导入会中途失败，
#    留下「一半有数据」的状态，比不恢复更难排查。
if [[ "$TARGET_EMPTY" == "1" ]]; then
  existing=$(psql_run -tAc \
    "select count(*) from information_schema.tables where table_schema='public'" | tr -d '[:space:]')
  if [[ "${existing:-0}" != "0" ]]; then
    echo "目标库 $POSTGRES_DB 的 public schema 已有 ${existing} 张表。" >&2
    echo "按 runbook 先清空（DROP SCHEMA public CASCADE; CREATE SCHEMA public;），" >&2
    echo "或换一个空库；确认要覆盖请设 TARGET_EMPTY=0。" >&2
    exit 1
  fi
fi

# 2) 按魔数选恢复方式。custom 格式前 5 字节是 PGDMP；不要靠扩展名判断——
#    实际产物里有叫 .dump 的，也有叫 .sql.gz 但内容是 custom 格式的历史残留。
magic=$(head -c 5 "$PG_FILE" 2>/dev/null || true)
echo "[1/2] 恢复 PostgreSQL: $PG_FILE (格式: $([[ "$magic" == "PGDMP" ]] && echo custom || echo plain))"
if [[ "$magic" == "PGDMP" ]]; then
  PGPASSWORD="${POSTGRES_PASSWORD:-}" pg_restore \
    -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --no-owner --no-privileges --exit-on-error "$PG_FILE"
else
  gunzip -c "$PG_FILE" | psql_run
fi

# 3) MinIO 可选：只恢复 PG（例如演练或只丢元数据）时不必提供
if [[ -n "${MINIO_DIR:-}" ]]; then
  if [[ ! -d "$MINIO_DIR" ]]; then
    echo "MINIO_DIR 不是目录: $MINIO_DIR" >&2
    exit 1
  fi
  echo "[2/2] 恢复 MinIO: $MINIO_DIR -> $MINIO_ALIAS/$MINIO_BUCKET"
  mc mirror --overwrite "$MINIO_DIR" "$MINIO_ALIAS/$MINIO_BUCKET"
else
  echo "[2/2] 跳过 MinIO（未提供 MINIO_DIR）；若原件也在事故中丢失，必须从备份回灌"
fi

echo "恢复完成；Milvus 请使用 milvus-backup restore 或按 runbook 重建索引。"
echo "别忘了 runbook §4.2：清语义缓存、核对 EMBEDDING_DIMENSION 与索引状态。"
