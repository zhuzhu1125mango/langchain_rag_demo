#!/usr/bin/env bash
# 每日一致性巡检（只读）：用一次性容器跑 scripts/consistency_audit.py。
#
# 为什么用 `docker compose run --rm` 而不是宿主机 python：
#   - 复用 compose 里的 env_file / environment / 网络别名，凭据不必落到宿主机 .env；
#   - 一次性容器不碰正在服务的 backend 进程，也不留垃圾容器。
#
# 用法：
#   ./scripts/nightly_consistency.sh                  # 全量巡检
#   AUDIT_DIR=/var/log/rag-audit ./scripts/nightly_consistency.sh
#   ./scripts/nightly_consistency.sh --skip-minio     # 参数透传给巡检脚本
#
# 退出码（与巡检脚本一致，供 cron / 监控消费）：
#   0 一致    1 发现差异    2 巡检未完整执行
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

OUT_DIR="${AUDIT_DIR:-$ROOT_DIR/backups/audit}"
mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d)"
JSON_REL="consistency_$STAMP.json"
LOG="$OUT_DIR/consistency_$STAMP.log"

echo "[nightly] 一致性巡检开始 $(date '+%F %T')  输出目录 $OUT_DIR"

# MSYS_NO_PATHCONV：Git Bash 会把 -v 的容器路径改写成 Windows 路径
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps \
  -v "$OUT_DIR:/audit" \
  backend python scripts/consistency_audit.py \
  --json-out "/audit/$JSON_REL" "$@" 2>&1 | tee "$LOG"
code=${PIPESTATUS[0]}

echo "[nightly] 结束 $(date '+%F %T')  exit_code=$code  json=$OUT_DIR/$JSON_REL"
if [ "$code" -eq 2 ]; then
  echo "[nightly] 巡检未完整执行（采集失败），请先确认 Postgres/Milvus/MinIO/Redis 可达" >&2
fi
exit "$code"
