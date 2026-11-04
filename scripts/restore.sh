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

# 宿主机没有 psql / pg_restore 时（Windows Git Bash 默认就没有），借用 postgres 容器执行。
# backup.sh 早就有这个降级，但 restore.sh 一直缺——2026-11-04 的端到端演练第一次在
# Git Bash 上按 runbook 恢复时才发现：文档写的命令在这台机器上根本跑不起来。
: "${PG_CONTAINER:=postgres-prod}"

have() { command -v "$1" >/dev/null 2>&1; }

winpath() {
  if have cygpath; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

# MINIO_ONLY=1：只回灌原件，不动数据库（演练 / 单丢原件时用）
: "${MINIO_ONLY:=0}"

if [[ "$MINIO_ONLY" != "1" ]]; then
  if [[ -z "${PG_FILE:-}" ]]; then
    echo "必须指定 PG_FILE（backup.sh 的 .sql.gz，或 pg_dump -Fc 的 .dump）；只恢复原件请设 MINIO_ONLY=1" >&2
    exit 1
  fi
  if [[ ! -f "$PG_FILE" ]]; then
    echo "PG_FILE 不存在: $PG_FILE" >&2
    exit 1
  fi
fi

psql_run() {
  if have psql; then
    PGPASSWORD="${POSTGRES_PASSWORD:-}" psql \
      -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      -v ON_ERROR_STOP=1 "$@"
  else
    # 容器模式：用容器自身的凭据与 unix socket，不需要把密码带到宿主机命令行
    docker exec -i "$PG_CONTAINER" \
      psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"
  fi
}

if [[ "$MINIO_ONLY" == "1" ]]; then
  echo "[1/2] 跳过 PostgreSQL（MINIO_ONLY=1）"
else
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
#    读前几个字节走管道再进变量：gzip 头第 4 字节是 0x00，直接 $(head -c 5) 会让
#    bash 打一句 "ignored null byte in input"，在恢复现场看起来像是出了问题。
magic=$(head -c 5 "$PG_FILE" 2>/dev/null | tr -d '\000' || true)
echo "[1/2] 恢复 PostgreSQL: $PG_FILE (格式: $([[ "$magic" == "PGDMP" ]] && echo custom || echo plain))"
if [[ "$magic" == "PGDMP" ]]; then
  if have pg_restore; then
    PGPASSWORD="${POSTGRES_PASSWORD:-}" pg_restore \
      -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      --no-owner --no-privileges --exit-on-error "$PG_FILE"
  else
    echo "      借用容器 $PG_CONTAINER（宿主机未安装 pg_restore）"
    docker cp "$(winpath "$PG_FILE")" "$PG_CONTAINER:/tmp/restore.dump"
    docker exec "$PG_CONTAINER" \
      pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      --no-owner --no-privileges --exit-on-error /tmp/restore.dump
    docker exec "$PG_CONTAINER" rm -f /tmp/restore.dump
  fi
else
  if have psql; then
    # -o /dev/null：pg_dump 里会有 SELECT set_config(...) 之类的语句，结果表会打到屏幕上，
    # 恢复现场看到一张空表容易误判成报错。
    gunzip -c "$PG_FILE" | psql_run -q -o /dev/null
  else
    echo "      借用容器 $PG_CONTAINER（宿主机未安装 psql）"
    docker cp "$(winpath "$PG_FILE")" "$PG_CONTAINER:/tmp/restore.sql.gz"
    # 目标库/用户必须由本脚本显式传进去，不能在单引号里留 $POSTGRES_DB 让容器的 sh 去展开：
    # 那样取到的是**容器自己的** POSTGRES_DB，调用方指定别的库时会静默导进错的库
    # （psql 照样 exit 0，看起来「恢复成功」而目标库仍是空的）。
    docker exec -e "PGU=$POSTGRES_USER" -e "PGD=$POSTGRES_DB" "$PG_CONTAINER" sh -c '
      gunzip -c /tmp/restore.sql.gz | psql -U "$PGU" -d "$PGD" -q -o /dev/null -v ON_ERROR_STOP=1
    '
    docker exec "$PG_CONTAINER" rm -f /tmp/restore.sql.gz
  fi
fi

fi  # MINIO_ONLY

# 3) MinIO 可选：只恢复 PG（例如演练或只丢元数据）时不必提供
if [[ -n "${MINIO_DIR:-}" ]]; then
  if [[ ! -d "$MINIO_DIR" ]]; then
    echo "MINIO_DIR 不是目录: $MINIO_DIR" >&2
    exit 1
  fi
  echo "[2/2] 恢复 MinIO: $MINIO_DIR -> $MINIO_ALIAS/$MINIO_BUCKET"
  if command -v mc >/dev/null 2>&1; then
    mc mirror --overwrite "$MINIO_DIR" "$MINIO_ALIAS/$MINIO_BUCKET"
  else
    # 宿主机没有 mc（Windows Git Bash 默认没有）：借 minio 容器执行。
    # 注意容器内地址是 127.0.0.1:9000，不是宿主机映射的 9002。
    : "${MINIO_CONTAINER:=minio-prod}"
    : "${MINIO_INTERNAL_URL:=http://127.0.0.1:9000}"
    echo "      借用容器 $MINIO_CONTAINER（宿主机未安装 mc）"
    # docker cp 的宿主机路径要显式转成 Windows 形式：调用方若设了 MSYS_NO_PATHCONV=1
    # （为保住容器内 /tmp 路径不被改写），该变量会被继承进来，`/c/...` 就变成了容器路径。
    if command -v cygpath >/dev/null 2>&1; then
      host_dir="$(cygpath -w "$MINIO_DIR")"
    else
      host_dir="$MINIO_DIR"
    fi
    docker exec "$MINIO_CONTAINER" rm -rf /tmp/restore_src >/dev/null 2>&1 || true
    docker cp "$host_dir/." "$MINIO_CONTAINER:/tmp/restore_src/"
    docker exec "$MINIO_CONTAINER" sh -c "
      set -e
      u=\$(cat /run/secrets/MINIO_ROOT_USER 2>/dev/null || echo \"\$MINIO_ROOT_USER\")
      p=\$(cat /run/secrets/MINIO_ROOT_PASSWORD 2>/dev/null || echo \"\$MINIO_ROOT_PASSWORD\")
      mc alias set bklocal '$MINIO_INTERNAL_URL' \"\$u\" \"\$p\" >/dev/null
      mc mb --ignore-existing 'bklocal/$MINIO_BUCKET' >/dev/null
      mc mirror --overwrite /tmp/restore_src 'bklocal/$MINIO_BUCKET'
      rm -rf /tmp/restore_src
    "
  fi
else
  echo "[2/2] 跳过 MinIO（未提供 MINIO_DIR）；若原件也在事故中丢失，必须从备份回灌"
fi

echo "恢复完成；Milvus 请使用 milvus-backup restore 或按 runbook 重建索引。"
echo "别忘了 runbook §4.2：清语义缓存、核对 EMBEDDING_DIMENSION 与索引状态。"
