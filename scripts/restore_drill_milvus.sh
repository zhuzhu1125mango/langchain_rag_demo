#!/usr/bin/env bash
# SEC-13：Milvus 快照恢复演练（整实例丢失 -> 全新实例）。
#
# 背景（2026-11-04）：PG 与 MinIO 的恢复演练都做完了，只有 Milvus 一直写着「未验，
# 无 milvus-backup 产物，0 向量可重建」。但「可重建」意味着要重新灌库、重新计算 embedding，
# 成本远高于前两者，不能靠「反正能重建」糊过去。
#
# 用官方 milvus-backup（milvusdb/milvus-backup）而不是自研导出：
#   自研只能导「我能想到的字段」，官方工具走的是 Milvus 自己的 binlog + meta，
#   恢复出来的东西才是真的「和原来一样」。
#
# 用法：
#   ./scripts/restore_drill_milvus.sh          # 全流程，结束自动清理
#   KEEP=1 ./scripts/restore_drill_milvus.sh   # 保留 drill-* 容器，便于人工检查
#
# 注意：**源就是生产 Milvus**（真实场景就是它出事）。脚本会在源实例建一个
# 独立集合 `drill_docs`（1000 条：128 维 dense + sparse 稀疏向量），结束时删除；不碰 documents。
#
# 为什么必须带 sparse（2026-11-05）：`documents` 有两个向量字段——dense（HNSW/COSINE）
# 与 sparse（SPARSE_INVERTED_INDEX/IP，BM25 词权重）。早先的演练集合只有 dense，
# 于是文档里只能写「稀疏索引未验，问答走混合检索间接覆盖」。间接覆盖不算证据：
# 混合检索会把 dense 的结果兜住，稀疏索引真丢了也可能看不出来。
#
# 三个已经踩过的坑，改这个脚本前务必读：
#   1) 不要给 drill-milvus 设 `COMMON_STORAGETYPE=minio`：2.6.17 会 panic
#      （`Failed to init arrow filesystem: Unsupported storage type: minio`），
#      默认本来就是 minio，设了反而错。
#   2) **restore 默认不重建索引**。恢复完集合在、数据在、`num_entities` 正确，
#      但 `indexes` 为空，`collection.load()` 直接报 `index not found`——
#      而日志写的是 `restore backup success`。必须带 `--rebuild_index`。
#   3) create 时若 config 里没配 etcd，`enable_dynamic_field` 的集合会被静默跳过
#      （`skip backup collection dynamic field, etcd client is not initialized`）。
#      本项目两个集合都没启用 dynamic field，所以无影响；以后若启用，必须补 etcd 段。
set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
WORK_DIR="$ROOT_DIR/backups/drill-milvus"

SRC_MILVUS="${SRC_MILVUS:-milvus-standalone-prod}"
SRC_MINIO="${SRC_MINIO:-minio-prod}"
BACKEND="${BACKEND:-backend-prod}"
NETWORK="${NETWORK:-rag-prod_infra-net}"
BACKUP_TOOL_IMAGE="${BACKUP_TOOL_IMAGE:-milvusdb/milvus-backup:latest}"
MILVUS_IMAGE="${MILVUS_IMAGE:-milvusdb/milvus:v2.6.17}"
ETCD_IMAGE="${ETCD_IMAGE:-quay.io/coreos/etcd:v3.5.30}"
MINIO_IMAGE="${MINIO_IMAGE:-pgsty/minio:RELEASE.2026-06-18T00-00-00Z}"

DRILL_ETCD="drill-etcd"
DRILL_MINIO="drill-minio"
DRILL_MILVUS="drill-milvus"
DRILL_VOL_ETCD="drill_etcd_milvus"
DRILL_VOL_MINIO="drill_minio_milvus"
DRILL_VOL_MILVUS="drill_milvus_data"

COLL="drill_docs"                       # 演练集合（源实例里的临时数据，结束即删）
BACKUP_BUCKET="milvus-backup-drill"     # 备份落到源 MinIO 的独立 bucket
# 备份名与集合名同规则（字母数字下划线），用连字符会被拒：
# `Error: invalid backup name drill-20261104-155448`
BACKUP_NAME="drill_${STAMP}"

DRILL_USER="drill_minio"
# `|| true` 必需：pipefail 下 head 关管道会让 tr 收到 SIGPIPE(141)，赋值失败 -> set -e
# 在打出任何一行之前退出（表现为「exit 0 + 输出全空」）。
DRILL_PASS="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 24 || true)"

step() { echo; echo "=== $* ==="; }

cleanup() {
  local rc=$?
  if [[ "${KEEP:-0}" == "1" ]]; then
    echo "[keep] 保留 drill-* 容器与 $WORK_DIR（drill_minio / $DRILL_PASS）"
    return $rc
  fi
  echo "--- 清理 ---"
  docker rm -f "$DRILL_MILVUS" "$DRILL_MINIO" "$DRILL_ETCD" >/dev/null 2>&1 || true
  docker volume rm "$DRILL_VOL_MILVUS" "$DRILL_VOL_MINIO" "$DRILL_VOL_ETCD" >/dev/null 2>&1 || true
  # 源实例上的演练集合与备份 bucket
  if docker inspect "$BACKEND" >/dev/null 2>&1; then
    docker exec -i "$BACKEND" python -c "
from pymilvus import connections, utility
try:
    connections.connect(host='$SRC_MILVUS', port='19530', timeout=5)
    if utility.has_collection('$COLL'):
        utility.drop_collection('$COLL')
        print('  已删除演练集合 $COLL')
except Exception as e:
    print('  清理集合失败（可手工删）:', str(e)[:100])
" 2>/dev/null || true
  fi
  if docker inspect "$SRC_MINIO" >/dev/null 2>&1; then
    docker exec "$SRC_MINIO" sh -c '
      u=$(cat /run/secrets/MINIO_ROOT_USER 2>/dev/null || echo "$MINIO_ROOT_USER")
      p=$(cat /run/secrets/MINIO_ROOT_PASSWORD 2>/dev/null || echo "$MINIO_ROOT_PASSWORD")
      mc alias set bk http://127.0.0.1:9000 "$u" "$p" >/dev/null 2>&1 || exit 0
      mc rb --force "bk/'"$BACKUP_BUCKET"'" >/dev/null 2>&1 || true
    ' 2>/dev/null || true
  fi
  rm -rf "$WORK_DIR"
  echo "已清理（drill 容器/卷、演练集合、备份 bucket、本地工作目录）"
  return $rc
}
trap cleanup EXIT

# 通过 backend 容器执行一段 python（它已经装了 pymilvus，不必再造一个环境）。
# -W ignore：pymilvus 3.0 的 ORM 接口会逐行打 DeprecationWarning，把校验结果淹没掉。
py() { docker exec -i "$BACKEND" python -W ignore -; }

step "0/7 前置检查"
for c in "$SRC_MILVUS" "$SRC_MINIO" "$BACKEND"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "缺少容器: $c" >&2; exit 1; }
done
docker image inspect "$BACKUP_TOOL_IMAGE" >/dev/null 2>&1 || {
  echo "缺少备份工具镜像 $BACKUP_TOOL_IMAGE" >&2
  echo "  docker pull $BACKUP_TOOL_IMAGE" >&2
  exit 1
}
mkdir -p "$WORK_DIR"
echo "源实例: $SRC_MILVUS（会在其上加临时集合 $COLL）"
echo "备份工具: $BACKUP_TOOL_IMAGE"

step "1/7 在源实例造演练集合（$COLL，1000 条：128 维 dense + sparse，各建索引）"
py <<PY
import random
from pymilvus import connections, utility, Collection, CollectionSchema, FieldSchema, DataType

connections.connect(host="$SRC_MILVUS", port="19530")
if utility.has_collection("$COLL"):
    utility.drop_collection("$COLL")
fields = [
    FieldSchema("id", DataType.INT64, is_primary=True, auto_id=False),
    FieldSchema("text", DataType.VARCHAR, max_length=512),
    FieldSchema("category", DataType.VARCHAR, max_length=64),
    FieldSchema("vec", DataType.FLOAT_VECTOR, dim=128),
    # 与 documents 同构：稀疏字段 + SPARSE_INVERTED_INDEX/IP（BM25 权重形态）
    FieldSchema("sparse", DataType.SPARSE_FLOAT_VECTOR),
]
col = Collection("$COLL", CollectionSchema(fields, description="drill only"), consistency_level="Strong")
random.seed(42)
ids = list(range(1000))
dense = [[random.random() for _ in range(128)] for _ in ids]
# 每行 3~8 个非零维，维度落在 30000 内（模拟词表规模），值 0.1~1.0
random.seed(43)
sparse = []
for _ in ids:
    dims = random.sample(range(30000), random.randint(3, 8))
    sparse.append({d: round(random.uniform(0.1, 1.0), 4) for d in dims})
col.insert([ids, [f"drill document {i}" for i in ids],
            [("alpha", "beta", "gamma")[i % 3] for i in ids],
            dense, sparse])
col.flush()
col.create_index("vec", {"index_type": "HNSW", "metric_type": "COSINE",
                         "params": {"M": 8, "efConstruction": 64}})
col.create_index("sparse", {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "IP"})
col.load()
print(f"  entities={col.num_entities}")
print("  indexes=" + ", ".join(f"{i.field_name}:{i.params.get('index_type')}/{i.params.get('metric_type')}" for i in col.indexes))
PY

step "2/7 生成 backup.yaml（凭据取自 secrets/，不打印）"
# 由 python 生成内容、bash 负责重定向：Windows 版 python 不认 `/c/...` 这种 MSYS 路径，
# 让它直接写文件会 FileNotFoundError（os.path.join 还会拼成反斜杠混合路径）。
MINIO_U="$(cat "$ROOT_DIR/secrets/minio_root_user")"
MINIO_P="$(cat "$ROOT_DIR/secrets/minio_root_password")"
CFG_MILVUS="$SRC_MILVUS" CFG_MINIO="$SRC_MINIO" CFG_BUCKET="$BACKUP_BUCKET" \
CFG_U="$MINIO_U" CFG_P="$MINIO_P" python - <<'PY' > "$WORK_DIR/backup.yaml"
import os
e = os.environ
cfg = f"""configVersion: v2
log:
  level: info
  console: true
  file:
    path: ""
milvus:
  user: "root"
  password: "Milvus"
  grpc:
    address: {e['CFG_MILVUS']}
    port: 19530
    tlsMode: disabled
  storage:
    provider: "minio"
    address: {e['CFG_MINIO']}
    port: 9000
    region: ""
    useSSL: false
    bucketName: "a-bucket"
    rootPath: "files"
    auth:
      type: static
      accessKeyID: {e['CFG_U']}
      secretAccessKey: {e['CFG_P']}
backup:
  storage:
    provider: "minio"
    address: {e['CFG_MINIO']}
    port: 9000
    region: ""
    useSSL: false
    bucketName: "{e['CFG_BUCKET']}"
    rootPath: "backup"
    auth:
      type: static
      accessKeyID: {e['CFG_U']}
      secretAccessKey: {e['CFG_P']}
"""
print(cfg, end="")
PY
echo "  已写入 $WORK_DIR/backup.yaml"

step "3/7 执行备份（milvus-backup create）"
docker exec "$SRC_MINIO" sh -c '
  u=$(cat /run/secrets/MINIO_ROOT_USER 2>/dev/null || echo "$MINIO_ROOT_USER")
  p=$(cat /run/secrets/MINIO_ROOT_PASSWORD 2>/dev/null || echo "$MINIO_ROOT_PASSWORD")
  mc alias set bk http://127.0.0.1:9000 "$u" "$p" >/dev/null
  mc mb --ignore-existing "bk/'"$BACKUP_BUCKET"'" >/dev/null
'
BACKUP_START=$(date +%s)
# 输出落文件而不是直接管道给 grep：pipefail 下 grep 没匹配到就会让整个脚本静默退出，
# 连错误原因都看不到。
if ! docker run --rm --network "$NETWORK" \
  -v "$(cygpath -w "$WORK_DIR" 2>/dev/null || echo "$WORK_DIR"):/cfg" \
  --entrypoint /app/milvus-backup "$BACKUP_TOOL_IMAGE" \
  --config /cfg/backup.yaml create -n "$BACKUP_NAME" --filter "$COLL" \
  > "$WORK_DIR/create.log" 2>&1; then
  echo "备份失败，日志尾部：" >&2
  tail -25 "$WORK_DIR/create.log" >&2
  exit 1
fi
grep -E "create backup success|duration:" "$WORK_DIR/create.log" | sed 's/^/  /' || true
BACKUP_SECS=$(( $(date +%s) - BACKUP_START ))
echo "  备份耗时 ${BACKUP_SECS}s"

step "4/7 起全新一套 Milvus（新 etcd + 新 MinIO + 新 Milvus）"
docker rm -f "$DRILL_MILVUS" "$DRILL_MINIO" "$DRILL_ETCD" >/dev/null 2>&1 || true
for v in "$DRILL_VOL_MILVUS" "$DRILL_VOL_MINIO" "$DRILL_VOL_ETCD"; do
  docker volume rm "$v" >/dev/null 2>&1 || true
done
DRILL_START=$(date +%s)
docker run -d --name "$DRILL_ETCD" --network "$NETWORK" -v "$DRILL_VOL_ETCD:/etcd" "$ETCD_IMAGE" \
  etcd -advertise-client-urls=http://"$DRILL_ETCD":2379 -listen-client-urls http://0.0.0.0:2379 \
  --data-dir /etcd >/dev/null
docker run -d --name "$DRILL_MINIO" --network "$NETWORK" \
  -e MINIO_ROOT_USER="$DRILL_USER" -e MINIO_ROOT_PASSWORD="$DRILL_PASS" \
  -e MINIO_ROOT_USER_FILE= -e MINIO_ROOT_PASSWORD_FILE= \
  -e MINIO_ACCESS_KEY_FILE= -e MINIO_SECRET_KEY_FILE= \
  -v "$DRILL_VOL_MINIO:/data" "$MINIO_IMAGE" server /data >/dev/null
sleep 6
docker exec "$DRILL_MINIO" sh -c "mc alias set bk http://127.0.0.1:9000 '$DRILL_USER' '$DRILL_PASS' >/dev/null \
  && mc mb --ignore-existing bk/a-bucket && mc mb --ignore-existing bk/$BACKUP_BUCKET" >/dev/null
# 把备份数据搬到新实例（跨实例 mc mirror，生产 MinIO 里两个 alias 都有 mc）
docker exec -e DRILLPASS="$DRILL_PASS" "$SRC_MINIO" sh -c "
  u=\$(cat /run/secrets/MINIO_ROOT_USER 2>/dev/null || echo \"\$MINIO_ROOT_USER\")
  p=\$(cat /run/secrets/MINIO_ROOT_PASSWORD 2>/dev/null || echo \"\$MINIO_ROOT_PASSWORD\")
  mc alias set bk http://127.0.0.1:9000 \"\$u\" \"\$p\" >/dev/null
  mc alias set drill http://$DRILL_MINIO:9000 '$DRILL_USER' \"\$DRILLPASS\" >/dev/null
  mc mirror --overwrite bk/$BACKUP_BUCKET drill/$BACKUP_BUCKET
" >/dev/null 2>&1
# 坑 1：不要设 COMMON_STORAGETYPE=minio，2.6.17 会 panic
docker run -d --name "$DRILL_MILVUS" --network "$NETWORK" \
  -e ETCD_ENDPOINTS="$DRILL_ETCD:2379" \
  -e MINIO_ADDRESS="$DRILL_MINIO:9000" \
  -e MINIO_ACCESS_KEY_ID="$DRILL_USER" \
  -e MINIO_SECRET_ACCESS_KEY="$DRILL_PASS" \
  -e MINIO_BUCKET_NAME=a-bucket \
  -v "$DRILL_VOL_MILVUS:/var/lib/milvus" "$MILVUS_IMAGE" milvus run standalone >/dev/null
ready=0
for i in $(seq 1 30); do
  if py <<PY 2>/dev/null | grep -q READY
from pymilvus import connections, utility
try:
    connections.connect(host="$DRILL_MILVUS", port="19530", timeout=5)
    utility.list_collections()
    print("READY")
except Exception:
    pass
PY
  then ready=1; echo "  新实例就绪（约 $((i*4))s）"; break; fi
  sleep 4
done
[[ "$ready" == "1" ]] || { echo "新实例未就绪，日志：" >&2; docker logs --tail 20 "$DRILL_MILVUS" >&2; exit 1; }
before=$(py <<PY 2>/dev/null | tail -1
from pymilvus import connections, utility
connections.connect(host="$DRILL_MILVUS", port="19530")
print(len(utility.list_collections()))
PY
)
echo "  恢复前集合数：${before:-?}（应为 0）"

step "5/7 生成 restore.yaml 并恢复（--rebuild_index 不能省）"
CFG_MILVUS="$DRILL_MILVUS" CFG_MINIO="$DRILL_MINIO" CFG_BUCKET="$BACKUP_BUCKET" \
CFG_U="$DRILL_USER" CFG_P="$DRILL_PASS" python - <<'PY' > "$WORK_DIR/restore.yaml"
import os
e = os.environ
cfg = f"""configVersion: v2
log:
  level: info
  console: true
  file:
    path: ""
milvus:
  user: "root"
  password: "Milvus"
  grpc:
    address: {e['CFG_MILVUS']}
    port: 19530
    tlsMode: disabled
  storage:
    provider: "minio"
    address: {e['CFG_MINIO']}
    port: 9000
    region: ""
    useSSL: false
    bucketName: "a-bucket"
    rootPath: "files"
    auth:
      type: static
      accessKeyID: {e['CFG_U']}
      secretAccessKey: {e['CFG_P']}
backup:
  storage:
    provider: "minio"
    address: {e['CFG_MINIO']}
    port: 9000
    region: ""
    useSSL: false
    bucketName: "{e['CFG_BUCKET']}"
    rootPath: "backup"
    auth:
      type: static
      accessKeyID: {e['CFG_U']}
      secretAccessKey: {e['CFG_P']}
"""
print(cfg, end="")
PY
RESTORE_START=$(date +%s)
if ! docker run --rm --network "$NETWORK" \
  -v "$(cygpath -w "$WORK_DIR" 2>/dev/null || echo "$WORK_DIR"):/cfg" \
  --entrypoint /app/milvus-backup "$BACKUP_TOOL_IMAGE" \
  --config /cfg/restore.yaml restore -n "$BACKUP_NAME" --rebuild_index \
  > "$WORK_DIR/restore.log" 2>&1; then
  echo "恢复失败，日志尾部：" >&2
  tail -25 "$WORK_DIR/restore.log" >&2
  exit 1
fi
grep -E "restore backup success|duration:" "$WORK_DIR/restore.log" | sed 's/^/  /' || true
RESTORE_END=$(date +%s)
echo "  恢复耗时 $(( RESTORE_END - RESTORE_START ))s"

step "6/7 校验：条数 / 双索引 / 标量与两种向量哈希 / dense 与 sparse 检索"
py <<PY
import hashlib
from pymilvus import connections, Collection

def probe(alias, host):
    connections.connect(alias=alias, host=host, port="19530")
    c = Collection("$COLL", using=alias)
    c.load()
    idx = sorted(f"{i.field_name}:{i.params.get('index_type')}/{i.params.get('metric_type')}" for i in c.indexes)
    rows = c.query(expr="id < 100", output_fields=["id", "text", "category", "vec", "sparse"], using=alias)
    rows.sort(key=lambda r: r["id"])
    # 稀疏向量也要进哈希：只比 dense 的话，稀疏数据丢了照样「一致」
    blob = "|".join(
        f"{r['id']}:{r['text']}:{r['category']}:"
        + ",".join(f"{v:.6f}" for v in r["vec"])
        + ":"
        + ",".join(f"{k}={r['sparse'][k]:.4f}" for k in sorted(r["sparse"]))
        for r in rows
    )
    hits = c.search([[0.5] * 128], "vec", {"metric_type": "COSINE", "params": {"ef": 64}}, limit=5, using=alias)
    # 用同一行（id=0）的稀疏维造查询串，两侧一致、可复现
    q = {k: 1.0 for k in list(sorted(rows[0]["sparse"]))[:5]}
    shits = c.search([q], "sparse", {"metric_type": "IP", "params": {}}, limit=5, using=alias)
    return dict(n=c.num_entities, idx=idx, sampled=len(rows),
                hash=hashlib.sha256(blob.encode()).hexdigest()[:16],
                top=[h.id for h in hits[0]],
                stop=[h.id for h in shits[0]],
                sq=list(q))

src = probe("src", "$SRC_MILVUS")
dst = probe("dst", "$DRILL_MILVUS")
bad = 0
for k in ("n", "idx", "sampled", "hash", "top", "stop", "sq"):
    ok = src[k] == dst[k]
    bad += 0 if ok else 1
    print(("  OK   " if ok else "  DIFF ") + f"{k}: 源={src[k]} 目标={dst[k]}")
print(f"  sparse 索引存在: {'sparse:SPARSE_INVERTED_INDEX/IP' in src['idx']}")
raise SystemExit(1 if bad else 0)
PY

step "7/7 结果"
elapsed=$(( RESTORE_END - DRILL_START ))
echo "  备份大小：$(docker exec "$SRC_MINIO" sh -c 'u=$(cat /run/secrets/MINIO_ROOT_USER); p=$(cat /run/secrets/MINIO_ROOT_PASSWORD); mc alias set bk http://127.0.0.1:9000 "$u" "$p" >/dev/null; mc du "bk/'"$BACKUP_BUCKET"'"' 2>/dev/null | tail -1)"
echo "  备份 ${BACKUP_SECS}s / 恢复 $(( RESTORE_END - RESTORE_START ))s"
echo "  RTO（起实例 -> 恢复完成）：${elapsed}s"
echo "  说明：1000 条（128 维 dense + sparse）的样本，不能外推到真实数据量；建索引耗时随集合规模上升明显。"
echo "  稀疏索引：SPARSE_INVERTED_INDEX/IP 已随备份恢复，稀疏检索 top-k 与源实例一致（见 6/7）。"
