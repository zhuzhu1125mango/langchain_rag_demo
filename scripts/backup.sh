#!/usr/bin/env bash
# SEC-13：PostgreSQL + MinIO 备份（Milvus 使用 milvus-backup 单独执行，见 runbook）。
#
# 用法：BACKUP_DIR=/backup ./scripts/backup.sh
#
# 两种执行模式（自动选择）：
#   host   ：宿主机装了 pg_dump / mc —— 直连数据库与对象存储
#   docker ：宿主机没装（Windows Git Bash 默认就是这种）——借用 postgres/minio 容器里的
#            客户端执行。2026-11-04 发现：本机 pg_dump 与 mc 都不存在，脚本按 runbook
#            根本跑不起来，这才是「backups/ 里没有一个 .sql.gz」的真正原因。
#
# 覆盖项：PG_CONTAINER（默认 postgres-prod）、MINIO_CONTAINER（默认 minio-prod）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:=app_prod}"
: "${POSTGRES_USER:=prod_user}"
: "${MINIO_ALIAS:=local}"
: "${MINIO_BUCKET:=documents}"
: "${PG_CONTAINER:=postgres-prod}"
: "${MINIO_CONTAINER:=minio-prod}"
# 容器内 MinIO 的访问地址（服务端监听 9000，不是宿主机映射的 9002）
: "${MINIO_INTERNAL_URL:=http://127.0.0.1:9000}"

have() { command -v "$1" >/dev/null 2>&1; }

# docker cp 的宿主机路径：Git Bash 下 `/c/...` 需要转成 `C:\...`，否则 docker 会当成
# 容器内路径。不能依赖 MSYS_NO_PATHCONV——调用方（如 restore_drill_minio.sh）为了保住
# 容器内 /tmp 路径会把它设成 1，环境变量会被继承进来，于是 docker cp 报
# 「directory "C:\c\MyCode\..." does not exist」。显式转换最稳。
winpath() {
  if have cygpath; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

# ---------- 1. PostgreSQL ----------
PG_OUT="$BACKUP_DIR/postgres_$STAMP.sql.gz"
if have pg_dump; then
  echo "[1/3] PostgreSQL dump（宿主机 pg_dump）-> $PG_OUT"
  PGPASSWORD="${POSTGRES_PASSWORD:-}" pg_dump \
    -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --no-owner --no-privileges | gzip > "$PG_OUT"
else
  echo "[1/3] PostgreSQL dump（借用容器 $PG_CONTAINER）-> $PG_OUT"
  # 凭据从容器自身的环境取，不经过宿主机命令行（避免进 shell 历史/进程列表）
  docker exec "$PG_CONTAINER" sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
       --no-owner --no-privileges' | gzip > "$PG_OUT"
fi

# ---------- 2. MinIO ----------
# BACKUP_SKIP_MINIO=1：只备份 PG（规模测试、或 MinIO 另行单独演练时用）。
# 少了这个开关，「量 PG 备份耗时」会被 MinIO 镜像时间污染。
: "${BACKUP_SKIP_MINIO:=0}"
if [[ "$BACKUP_SKIP_MINIO" == "1" ]]; then
  echo "[2/3] 跳过 MinIO（BACKUP_SKIP_MINIO=1）"
  MINIO_OUT=""
else
MINIO_OUT="$BACKUP_DIR/minio_$STAMP"
mkdir -p "$MINIO_OUT"
if have mc; then
  echo "[2/3] MinIO mirror（宿主机 mc）-> $MINIO_OUT/"
  mc mirror --overwrite "$MINIO_ALIAS/$MINIO_BUCKET" "$MINIO_OUT"
else
  echo "[2/3] MinIO mirror（借用容器 $MINIO_CONTAINER）-> $MINIO_OUT/"
  # 容器里 mc 已存在，但别名与目标目录要现建：mirror 到 /tmp 再用 docker cp 取回宿主机
  # 凭据优先取 Docker Secrets（生产是 *_FILE 注入，环境变量里并没有 MINIO_ROOT_*），
  # 没有再用环境变量——开发栈是直接给环境变量的。
  docker exec "$MINIO_CONTAINER" sh -c "
    set -e
    u=\$(cat /run/secrets/MINIO_ROOT_USER 2>/dev/null || echo \"\$MINIO_ROOT_USER\")
    p=\$(cat /run/secrets/MINIO_ROOT_PASSWORD 2>/dev/null || echo \"\$MINIO_ROOT_PASSWORD\")
    mc alias set bklocal '$MINIO_INTERNAL_URL' \"\$u\" \"\$p\" >/dev/null
    rm -rf /tmp/backup_mirror && mkdir -p /tmp/backup_mirror
    mc mirror --overwrite 'bklocal/$MINIO_BUCKET' /tmp/backup_mirror
  "
  # 空 bucket 时 mirror 不产生文件，docker cp 会报错——先判断，避免把「没有对象」
  # 误报成「备份失败」。空备份本身是合法结果（当前生产就是 0 对象）。
  if docker exec "$MINIO_CONTAINER" sh -c '[ -n "$(ls -A /tmp/backup_mirror 2>/dev/null)" ]'; then
    docker cp "$MINIO_CONTAINER:/tmp/backup_mirror/." "$(winpath "$MINIO_OUT")/"
  fi
  docker exec "$MINIO_CONTAINER" rm -rf /tmp/backup_mirror
fi
fi   # BACKUP_SKIP_MINIO

# ---------- 3. Milvus ----------
echo "[3/3] Milvus backup（如已配置 milvus-backup）"
if have milvus-backup; then
  milvus-backup create -n "backup_$STAMP"
else
  echo "  未安装 milvus-backup，跳过；请按 docs/guide/backup-restore.md 手动快照。"
fi

# ---------- 产物自检（runbook §3.2：未通过校验的备份按「无备份」处理）----------
echo "--- 产物校验 ---"
gzip -t "$PG_OUT"
pg_size=$(du -h "$PG_OUT" | cut -f1)
echo "  PG   : $PG_OUT ($pg_size, gzip 完整)"
if [[ -z "$MINIO_OUT" ]]; then
  echo "  MinIO: 已跳过"
  echo "备份完成：$BACKUP_DIR（$STAMP）"
  exit 0
fi
obj_count=$(find "$MINIO_OUT" -type f | wc -l | tr -d '[:space:]')
echo "  MinIO: $obj_count 个对象 -> $MINIO_OUT"
if [[ "$obj_count" == "0" ]]; then
  echo "  注意：bucket「$MINIO_BUCKET」当前没有对象，镜像为空属正常；若预期有对象请先排查。"
fi

echo "备份完成：$BACKUP_DIR（$STAMP）"
