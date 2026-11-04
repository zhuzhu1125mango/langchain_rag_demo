#!/usr/bin/env bash
# SEC-13：MinIO「整实例丢失」恢复演练。
#
# 与 restore_drill.sh（PostgreSQL）对称：那个验证「库还能不能恢复」，这个验证
# 「对象存储整个实例没了，能不能从备份镜像把原件恢复到一台全新实例」。
#
# 为什么要单独一个脚本（2026-11-04）：
#   第一次 MinIO 演练只是在**同一个实例内换 bucket**（drill-src -> drill-dst），
#   同实例意味着凭据、版本、配置全都还在——最坏的情况（实例连数据卷一起丢）没被验证过。
#   而原件不可重建，是真正丢不起的那一半。
#
# 用法：
#   ./scripts/restore_drill_minio.sh          # 全流程，结束自动清理
#   KEEP=1 ./scripts/restore_drill_minio.sh   # 保留容器与新实例，便于人工检查
#
# 只碰 drill-* 命名的资源：容器 drill-minio、卷 drill_minio_data、源 bucket drill-src-*。
# 不碰 documents，不碰生产 MinIO 的既有数据。
#
# 两条踩过的坑，改这个脚本前请先读：
#   1) MinIO 镜像的工具集极小（没有 grep/sed/wc/sha256sum）。**容器内只能跑 mc**，
#      所有文本处理与哈希都在宿主机做，需要本地副本就 `mc mirror` + `docker cp`。
#   2) 该镜像自带 MINIO_ROOT_USER_FILE=access_key 等默认值。只要这些 *_FILE 变量还在，
#      服务端就忽略 MINIO_ROOT_USER/PASSWORD 去读文件，凭据完全不是给的那份。
set -euo pipefail

# Windows Git Bash（MSYS2）会把以 / 开头的参数当成宿主机路径改写：
# `docker exec ... mc ls /tmp/x` 会被塞成 C:/Users/.../tmp/x。runbook 指定的执行环境
# 正是 Git Bash，所以显式关掉路径转换（docker cp 的宿主机路径因此要自己转 winpath）。
export MSYS_NO_PATHCONV=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"

SRC_CONTAINER="${SRC_CONTAINER:-minio-prod}"
DRILL_CONTAINER="${DRILL_CONTAINER:-drill-minio}"
DRILL_VOLUME="${DRILL_VOLUME:-drill_minio_data}"
DRILL_IMAGE="${DRILL_IMAGE:-pgsty/minio:RELEASE.2026-06-18T00-00-00Z}"
NETWORK="${NETWORK:-rag-prod_infra-net}"
# 恢复到同名 bucket（documents），因为真实事故里恢复出来的就是业务在用的那个名字
DST_BUCKET="${DST_BUCKET:-documents}"
# S3 bucket 名只允许小写字母、数字、连字符（不能有下划线），所以 STAMP 里的 _ 要换掉，
# 否则 mc mb 直接报「Bucket name contains invalid characters」。
SRC_BUCKET="drill-src-${STAMP//_/-}"
WORK_DIR="$ROOT_DIR/backups/drill-minio-$STAMP"

DRILL_USER="drill_minio"
# 末尾的 `|| true` 不能省：`set -o pipefail` 下，head 读够 24 字节就关管道，
# 上游 tr 收到 SIGPIPE 退出 141，赋值语句因此返回非零 → `set -e` 在打出任何一行
# 之前就把脚本干掉了（表现是「exit 0、输出全空」，极难定位）。
DRILL_PASS="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 24 || true)"

# 容器内 MinIO 都监听 9000（宿主机映射到 9002 的那个只是端口映射）
MC_URL="http://127.0.0.1:9000"

step() { echo; echo "=== $* ==="; }

winpath() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

cleanup() {
  local rc=$?
  if [[ "${KEEP:-0}" == "1" ]]; then
    echo "[keep] 保留 $DRILL_CONTAINER 与 $WORK_DIR（凭据 $DRILL_USER / $DRILL_PASS）"
    echo "[keep] 源 bucket: $SRC_BUCKET"
    return $rc
  fi
  echo "--- 清理 ---"
  docker rm -f "$DRILL_CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$DRILL_VOLUME" >/dev/null 2>&1 || true
  # trap 可能在 mc_in 定义之前就触发（前置检查失败时），先确认函数在
  if declare -F mc_in >/dev/null 2>&1; then
    mc_in "$SRC_CONTAINER" "mc rb --force 'bk/$SRC_BUCKET'" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK_DIR"
  echo "已清理（容器 / 卷 / 源 bucket / 本地镜像目录）"
  return $rc
}
trap cleanup EXIT

# 借容器执行 mc：凭据优先取 Docker Secrets（生产是 *_FILE 注入），否则用环境变量
# （演练实例是把凭据放在环境变量里的）。两边都成立，所以这一个函数够用。
# 注意：`$*` 必须是纯 mc 命令，不能带 sed/wc 之类——镜像里没有这些工具。
mc_in() {
  local container="$1"; shift
  docker exec -i "$container" sh -c "
    u=\$(cat /run/secrets/MINIO_ROOT_USER 2>/dev/null || echo \"\$MINIO_ROOT_USER\")
    p=\$(cat /run/secrets/MINIO_ROOT_PASSWORD 2>/dev/null || echo \"\$MINIO_ROOT_PASSWORD\")
    mc alias set bk '$MC_URL' \"\$u\" \"\$p\" >/dev/null
    $*
  "
}

# 把 bucket 内容拉到宿主机目录（mc mirror 到容器 /tmp，再 docker cp 出来）
pull_bucket() {
  local container="$1" bucket="$2" dest="$3"
  docker exec "$container" rm -rf /tmp/cmp_pull >/dev/null 2>&1 || true
  docker exec "$container" mkdir -p /tmp/cmp_pull
  mc_in "$container" "mc mirror --quiet 'bk/$bucket' /tmp/cmp_pull" >/dev/null
  mkdir -p "$dest"
  docker cp "$container:/tmp/cmp_pull/." "$(winpath "$dest")/" >/dev/null
  docker exec "$container" rm -rf /tmp/cmp_pull
}

# 输出「文件数 / 文件名集合哈希 / 内容哈希集合」，三行都可比
EMPTY_SHA="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # sha256("")
manifest() {
  local d="$1" files
  files=$(cd "$d" && find . -type f | wc -l | tr -d '[:space:]')
  echo "FILES=$files"
  echo "NAMES_SHA=$(cd "$d" && find . -type f -print | LC_ALL=C sort | sha256sum | cut -d' ' -f1)"
  # 不用 `xargs -0 sha256sum`：MSYS 的 xargs 在参数超长时直接断言崩溃
  # （assertion "bc_ctl.arg_max >= LINE_MAX" failed），管道输出为空，
  # 于是两侧的 CONTENT_SHA 都退化成「空串的 sha256」——看起来一致，其实什么都没比。
  # 校验值本身会退化成常量，是这类比对的通病，所以下面单独兜一道。
  echo "CONTENT_SHA=$(cd "$d" && find . -type f -print0 | LC_ALL=C sort -z \
    | while IFS= read -r -d '' f; do sha256sum "$f"; done \
    | awk '{print $1}' | LC_ALL=C sort | sha256sum | cut -d' ' -f1)"
}

step "0/7 前置检查"
docker inspect "$SRC_CONTAINER" >/dev/null 2>&1 || { echo "源容器不存在: $SRC_CONTAINER" >&2; exit 1; }
command -v docker >/dev/null || { echo "需要 docker" >&2; exit 1; }
echo "源实例: $SRC_CONTAINER（镜像 $(docker inspect "$SRC_CONTAINER" --format '{{.Config.Image}}')）"
echo "演练实例: $DRILL_CONTAINER / 卷 $DRILL_VOLUME（全新，凭据与生产不同）"
echo "网络: $NETWORK"

step "1/7 在源实例造种子对象（独立 bucket: $SRC_BUCKET）"
docker exec "$SRC_CONTAINER" sh -c "
  set -e
  u=\$(cat /run/secrets/MINIO_ROOT_USER 2>/dev/null || echo \"\$MINIO_ROOT_USER\")
  p=\$(cat /run/secrets/MINIO_ROOT_PASSWORD 2>/dev/null || echo \"\$MINIO_ROOT_PASSWORD\")
  mc alias set bk '$MC_URL' \"\$u\" \"\$p\" >/dev/null
  mc mb --ignore-existing 'bk/$SRC_BUCKET' >/dev/null
  rm -rf /tmp/drillseed
  mkdir -p '/tmp/drillseed/nested/子目录'
  printf '备份演练样本：中文文件名与内容\n2026-11-04\n' > '/tmp/drillseed/报告 2026.txt'
  head -c 1048576 /dev/urandom > /tmp/drillseed/vector.bin
  printf '嵌套路径对象\n' > '/tmp/drillseed/nested/子目录/深一层.txt'
  mc cp --recursive /tmp/drillseed/ 'bk/$SRC_BUCKET/' >/dev/null
  rm -rf /tmp/drillseed
"
echo "  源 bucket 内容："
mc_in "$SRC_CONTAINER" "mc ls --recursive 'bk/$SRC_BUCKET'" | sed 's/^/    /'

step "2/7 调用真实 backup.sh 产出备份镜像"
MINIO_BUCKET="$SRC_BUCKET" BACKUP_DIR="$WORK_DIR" PG_CONTAINER=postgres-prod \
  bash "$ROOT_DIR/scripts/backup.sh" 2>&1 | sed 's/^/  /'
MIRROR_DIR="$(ls -d "$WORK_DIR"/minio_* | tail -1)"
echo "  镜像目录: $MIRROR_DIR"
echo "  本地文件数: $(find "$MIRROR_DIR" -type f | wc -l | tr -d '[:space:]')"

step "3/7 启动全新 MinIO 实例（独立数据卷，凭据与生产不同）"
docker rm -f "$DRILL_CONTAINER" >/dev/null 2>&1 || true
docker volume rm "$DRILL_VOLUME" >/dev/null 2>&1 || true
DRILL_START=$(date +%s)
docker run -d --name "$DRILL_CONTAINER" \
  --network "$NETWORK" \
  -e MINIO_ROOT_USER="$DRILL_USER" \
  -e MINIO_ROOT_PASSWORD="$DRILL_PASS" \
  -e MINIO_ROOT_USER_FILE= \
  -e MINIO_ROOT_PASSWORD_FILE= \
  -e MINIO_ACCESS_KEY_FILE= \
  -e MINIO_SECRET_KEY_FILE= \
  -v "$DRILL_VOLUME:/data" \
  "$DRILL_IMAGE" server /data >/dev/null

ready=0
for i in $(seq 1 60); do
  if docker exec "$DRILL_CONTAINER" sh -c \
      "mc alias set bk '$MC_URL' '$DRILL_USER' '$DRILL_PASS' >/dev/null 2>&1" >/dev/null 2>&1; then
    echo "  新实例就绪（${i}s）"; ready=1; break
  fi
  sleep 1
done
if [[ "$ready" != "1" ]]; then
  echo "新实例 60s 内未就绪，日志：" >&2
  docker logs --tail 20 "$DRILL_CONTAINER" >&2 || true
  exit 1
fi

before=$(mc_in "$DRILL_CONTAINER" "mc ls bk" | wc -l | tr -d '[:space:]')
echo "  恢复前的新实例 bucket 数：${before:-0}（应为 0）"
if [[ "${before:-0}" != "0" ]]; then
  echo "新实例不是空的，演练前提不成立" >&2
  exit 1
fi

step "4/7 调用真实 restore.sh 回灌到全新实例"
MINIO_ONLY=1 MINIO_DIR="$MIRROR_DIR" MINIO_BUCKET="$DST_BUCKET" \
  MINIO_CONTAINER="$DRILL_CONTAINER" MINIO_INTERNAL_URL="$MC_URL" \
  bash "$ROOT_DIR/scripts/restore.sh" 2>&1 | sed 's/^/  /'

RESTORE_END=$(date +%s)
echo "  回灌后新实例内容："
mc_in "$DRILL_CONTAINER" "mc ls --recursive 'bk/$DST_BUCKET'" | sed 's/^/    /'

step "5/7 逐对象校验（文件数 / 文件名集合 / 内容哈希集合）"
pull_bucket "$SRC_CONTAINER" "$SRC_BUCKET" "$WORK_DIR/pull-src"
pull_bucket "$DRILL_CONTAINER" "$DST_BUCKET" "$WORK_DIR/pull-dst"
SRC_MANIFEST="$(manifest "$WORK_DIR/pull-src")"
DST_MANIFEST="$(manifest "$WORK_DIR/pull-dst")"
echo "  源  ($SRC_BUCKET):"; echo "$SRC_MANIFEST" | sed 's/^/    /'
echo "  目标($DST_BUCKET):"; echo "$DST_MANIFEST" | sed 's/^/    /'
if [[ "$SRC_MANIFEST" != "$DST_MANIFEST" ]]; then
  echo "校验失败：源与恢复后不一致" >&2
  exit 1
fi
# 两侧都等于空串哈希说明校验本身坏了（文件数为 0，或哈希管道没产出），不是「一致」。
src_files=$(echo "$SRC_MANIFEST" | grep '^FILES=' | cut -d= -f2)
src_content=$(echo "$SRC_MANIFEST" | grep '^CONTENT_SHA=' | cut -d= -f2)
if [[ "${src_files:-0}" == "0" || "$src_content" == "$EMPTY_SHA" ]]; then
  echo "校验无效：文件数=$src_files 内容哈希退化成空串哈希，请检查 manifest 逻辑" >&2
  exit 1
fi
echo "  ✅ 内容与文件名集合完全一致（$src_files 个对象）"

step "6/7 用真实业务客户端（minio SDK）读新实例"
# mc 能看到不等于应用能读到——路径风格、region、bucket 策略都可能不同。
docker exec -i -e DRILL_USER="$DRILL_USER" -e DRILL_PASS="$DRILL_PASS" backend-prod python - <<'PY'
import hashlib, os
from minio import Minio

client = Minio("drill-minio:9000", access_key=os.environ["DRILL_USER"],
               secret_key=os.environ["DRILL_PASS"], secure=False)
objs = list(client.list_objects("documents", recursive=True))
print(f"  SDK 列出 {len(objs)} 个对象，合计 {sum(o.size for o in objs)} 字节")
for o in sorted(objs, key=lambda x: x.object_name):
    data = client.get_object("documents", o.object_name).read()
    print(f"    {o.object_name}  {o.size}B  sha256={hashlib.sha256(data).hexdigest()[:16]}")
PY

step "7/7 结果"
elapsed=$((RESTORE_END - DRILL_START))
echo "  对象数     : $(echo "$DST_MANIFEST" | grep '^FILES=' | cut -d= -f2)"
echo "  RTO（起实例 → 回灌完成）: ${elapsed}s"
echo "  说明：这个数字只代表 3 个对象 / 1MB 的样本，不能外推到真实数据量。"
