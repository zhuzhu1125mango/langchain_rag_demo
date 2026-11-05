#!/usr/bin/env bash
# 规模 RTO 演练：在「够大」的数据量上量备份与恢复，回答「1 万文档要恢复多久」。
#
# 为什么需要它：此前所有 RTO 都是几百 chunk / 136KB 的量级（PG 6s、MinIO 4s、Milvus 37s），
# 文档里只能写「不能外推」。而「能不能恢复」和「多久能恢复」是两个问题，后者决定要不要
# 做双活、要不要缩短 RPO。本次在三套存储上各造一份接近真实的量，量出可外推的数字。
#
# 隔离原则：**不碰生产**。
#   - PG：起两台一次性 Postgres（dscale-pg-src / -dst），src 用真实备份恢复出 schema，
#     再灌合成数据；dump 与 restore 都指向它们。
#   - Milvus：整套独立（etcd + MinIO + Milvus 各两套），不往生产集合里塞 2 万条假向量。
#   - MinIO：两台一次性实例，对象在本地生成后用 mc 灌。
#
# 合成数据的边界（必须写进报告）：
#   - 文本是 repeat(md5(...))，**熵高于真实中文**，gzip 压缩率会比真实语料低（备份偏大）；
#   - 向量是随机数，只有体积与索引构建耗时与真实一致，**不反映检索质量**。
#
# 用法：
#   MODE=pg ./scripts/scale_rto_drill.sh          # 只跑 PG
#   MODE=milvus ./scripts/scale_rto_drill.sh
#   MODE=minio ./scripts/scale_rto_drill.sh
#   ./scripts/scale_rto_drill.sh                  # 三套都跑（默认量：1 万文档 / 20 万 trace / 2 万向量）
#   PG_DOCS=2000 PG_TRACES=20000 MILVUS_ROWS=2000 MODE=all ./scripts/scale_rto_drill.sh   # 小样试跑
#   KEEP=1 ...                                     # 保留容器排查
set -euo pipefail
export MSYS_NO_PATHCONV=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MODE="${MODE:-all}"
KEEP="${KEEP:-0}"
WORK="$REPO_ROOT/backups/scale-rto"
mkdir -p "$WORK"
SECRETS="$REPO_ROOT/secrets"

PG_DOCS="${PG_DOCS:-10000}"
PG_TRACES="${PG_TRACES:-200000}"
PG_SESSIONS="${PG_SESSIONS:-10000}"

MILVUS_ROWS="${MILVUS_ROWS:-20000}"
MILVUS_DIM="${MILVUS_DIM:-1024}"

MINIO_OBJECTS="${MINIO_OBJECTS:-1000}"
MINIO_SIZE_KB="${MINIO_SIZE_KB:-200}"

PG_SRC="dscale-pg-src"; PG_DST="dscale-pg-dst"
PG_SRC_PORT="${PG_SRC_PORT:-5551}"; PG_DST_PORT="${PG_DST_PORT:-5552}"
PG_DB="app_prod"; PG_USER="prod_user"

# 复用生产 infra 网络：探针要跑在 backend-prod 里（它装了 pymilvus），
# 而 backend-prod 不在这条自建网络上就连不到 drill 实例；往里接接口比改生产容器更麻烦。
NET="${NET:-rag-prod_infra-net}"
MSRC_ETCD="dscale-msrc-etcd"; MSRC_MINIO="dscale-msrc-minio"; MSRC_MILVUS="dscale-msrc-milvus"
MDST_ETCD="dscale-mdst-etcd"; MDST_MINIO="dscale-mdst-minio"; MDST_MILVUS="dscale-mdst-milvus"
MILVUS_COLL="scale_docs"
MILVUS_BUCKET="a-bucket"
BACKUP_BUCKET="milvus-scale-backup"
BACKUP_NAME="scale_$(date +%Y%m%d_%H%M%S)"

MINIO_SRC="dscale-minio-src"; MINIO_DST="dscale-minio-dst"
MINIO_SRC_PORT="${MINIO_SRC_PORT:-9011}"; MINIO_DST_PORT="${MINIO_DST_PORT:-9012}"

have() { command -v "$1" >/dev/null 2>&1; }
winpath() { if have cygpath; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
NULL_DEV="$(cygpath -w /dev/null 2>/dev/null || echo /dev/null)"
now() { date +%s; }
mb() { echo $(( $1 / 1024 / 1024 )); }

RESULTS="$WORK/results-$MODE.md"   # 按模式分文件：三个演练各跑一次，共用一个文件会互相覆盖
: > "$RESULTS"

cleanup() {
  if [[ "$KEEP" == "1" ]]; then
    echo "（KEEP=1，保留 dscale-* 容器与 $WORK；MinIO 凭据见 $WORK/drill_creds.txt）"
    return
  fi
  docker rm -f "$PG_SRC" "$PG_DST" >/dev/null 2>&1 || true
  docker rm -f "$MSRC_ETCD" "$MSRC_MINIO" "$MSRC_MILVUS" "$MDST_ETCD" "$MDST_MINIO" "$MDST_MILVUS" >/dev/null 2>&1 || true
  docker rm -f "$MINIO_SRC" "$MINIO_DST" >/dev/null 2>&1 || true
  docker volume rm dscale_pg_src dscale_pg_dst >/dev/null 2>&1 || true
  docker volume rm dscale_etcd_src dscale_minio_src_milvus dscale_milvus_src \
    dscale_etcd_dst dscale_minio_dst_milvus dscale_milvus_dst >/dev/null 2>&1 || true
  docker volume rm dscale_minio_src dscale_minio_dst >/dev/null 2>&1 || true
  # 只删自己建的网络；NET 默认是生产 infra 网络，删它等于把生产拆了
  [[ "${NET_OWNED:-0}" == "1" ]] && docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

start_pg() {  # name port suffix
  local name="$1" port="$2" suffix="$3"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "dscale_pg_$suffix" >/dev/null 2>&1 || true
  docker run -d --name "$1" --network "$NET" \
    -e "POSTGRES_PASSWORD=$(cat "$SECRETS/postgres_password")" \
    -e "POSTGRES_DB=$PG_DB" -e "POSTGRES_USER=$PG_USER" \
    -p "127.0.0.1:$port:5432" -v "dscale_pg_$suffix:/var/lib/postgresql/data" \
    postgres:16.14 >/dev/null
  for _ in $(seq 1 60); do
    docker exec "$name" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "PG $name 未就绪" >&2; return 1
}

pgdo() { docker exec -i "$PG_SRC" psql -U "$PG_USER" -d "$PG_DB" -q -v ON_ERROR_STOP=1 -f - ; }
pgq() { docker exec "$1" psql -U "$PG_USER" -d "$PG_DB" -tAc "$2" | tr -d '[:space:]'; }

# ---------------------------------------------------------------------------
# PG
# ---------------------------------------------------------------------------
run_pg() {
  echo
  echo "=== PG 规模演练：$PG_DOCS 文档 / $PG_SESSIONS 会话 / $PG_TRACES trace ==="
  docker network create "$NET" >/dev/null 2>&1 || true
  t0=$(now); start_pg "$PG_SRC" "$PG_SRC_PORT" src
  echo "  [1/6] 起 src（$(( $(now) - t0 ))s）"

  # 用真实备份恢复出 schema 与基准行（1 个 KB、1 个用户），再灌合成数据
  base=$(ls -t backups/*.dump backups/rollback-drill/backup/*.sql.gz 2>/dev/null | head -1)
  [[ -n "$base" ]] || { echo "找不到基准备份" >&2; return 1; }
  echo "        schema 来自: $base"
  PG_CONTAINER="$PG_SRC" PG_FILE="$base" POSTGRES_DB="$PG_DB" POSTGRES_USER="$PG_USER" \
    TARGET_EMPTY=1 bash scripts/restore.sh >/dev/null

  echo "  [2/6] 灌合成数据"
  t1=$(now)
  pgdo <<SQL
-- 文档：模拟 1 万个已处理完的 PDF
INSERT INTO documents (id, filename, file_path, file_type, size, kb_id, owner_id,
                       status, processing_status, processing_progress, chunks_count,
                       summary, created_at, updated_at)
SELECT gen_random_uuid(),
       'doc_' || g || '.pdf',
       'documents/' || md5(g::text) || '.pdf',
       'pdf', 200000 + (g % 500000),
       (SELECT id FROM knowledge_bases LIMIT 1),
       (SELECT owner_id FROM knowledge_bases LIMIT 1),
       'published'::document_status, 'completed'::processing_status, 100,
       20 + (g % 200),
       repeat('这是用于恢复演练的合成摘要文本，不含真实业务信息。', 6) || g,
       now() - (g || ' minutes')::interval, now()
FROM generate_series(1, $PG_DOCS) g;

-- 会话：每条带 6 轮消息（jsonb 随对话增长，是 PG 里第二大的表）
INSERT INTO sessions (id, user_id, title, messages, kb_ids, created_at, updated_at)
SELECT gen_random_uuid(),
       (SELECT owner_id FROM knowledge_bases LIMIT 1),
       '规模演练会话 ' || g,
       (SELECT jsonb_agg(jsonb_build_object(
                 'role', CASE WHEN k % 2 = 1 THEN 'user' ELSE 'assistant' END,
                 'content', repeat('这是一段用于规模演练的对话内容，模拟真实问答长度。', 12)))
          FROM generate_series(1, 6) k),
       json_build_array((SELECT id::text FROM knowledge_bases LIMIT 1))::json,
       now() - (g || ' minutes')::interval, now()
FROM generate_series(1, $PG_SESSIONS) g;

-- 问答轨迹：真实系统里增长最快的表（容量模型按 2~10KB/次估算）
INSERT INTO request_traces (id, session_id, user_id, question, resolved_question,
                            intent_decision, primary_mode, tool_calls, search_results,
                            kb_results, fallback_triggered, final_answer,
                            total_latency_ms, stages, token_usage, created_at)
SELECT gen_random_uuid()::text,
       gen_random_uuid()::text,
       (SELECT owner_id FROM knowledge_bases LIMIT 1),
       repeat(md5(g::text), 8) || ' 这个系统的备份策略是什么？',
       repeat(md5((g + 1)::text), 8) || ' 备份与恢复的顺序',
       json_build_object('mode', 'hybrid', 'tools', '[]'::json)::json,
       'hybrid', '[]'::json, '[]'::json,
       json_build_array(json_build_object('source', 'architecture.md', 'score', 0.82))::json,
       false,
       repeat('恢复时先停写、再导库、最后对齐 schema 并重建索引。' || md5(g::text), 12),
       800 + (g % 12000),
       jsonb_build_object('kb_retrieval', jsonb_build_object('kb_ms', 120, 'hits', 3),
                          'generation', jsonb_build_object('gen_ms', 900, 'tokens', 320),
                          'rerank', jsonb_build_object('rerank_ms', 60)),
       jsonb_build_object('prompt_tokens', 1200, 'completion_tokens', 320),
       now() - (g || ' seconds')::interval
FROM generate_series(1, $PG_TRACES) g;
SQL
  echo "        灌数据耗时 $(( $(now) - t1 ))s"

  echo "  [3/6] 统计"
  db_size=$(pgq "$PG_SRC" "select pg_size_pretty(pg_database_size('$PG_DB'))")
  rows=$(docker exec "$PG_SRC" psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "select (select count(*) from documents), (select count(*) from sessions), (select count(*) from request_traces)" | tr -d '[:space:]')
  echo "        库大小 $db_size；docs/sessions/traces = $rows"

  echo "  [4/6] 备份（真实 backup.sh，跳过 MinIO 以免污染计时）"
  bdir="$WORK/backup_$(date +%H%M%S)"
  t2=$(now)
  BACKUP_SKIP_MINIO=1 BACKUP_DIR="$bdir" PG_CONTAINER="$PG_SRC" \
    POSTGRES_DB="$PG_DB" POSTGRES_USER="$PG_USER" ./scripts/backup.sh >/dev/null
  t_backup=$(( $(now) - t2 ))
  dump=$(ls -t "$bdir"/*.sql.gz | head -1)
  dump_mb=$(du -m "$dump" | cut -f1)
  echo "        备份 ${t_backup}s，产物 ${dump_mb}MB"

  echo "  [5/6] 恢复到空库（真实 restore.sh）"
  start_pg "$PG_DST" "$PG_DST_PORT" dst
  t3=$(now)
  PG_CONTAINER="$PG_DST" PG_FILE="$dump" POSTGRES_DB="$PG_DB" POSTGRES_USER="$PG_USER" \
    TARGET_EMPTY=1 bash scripts/restore.sh >/dev/null
  t_restore=$(( $(now) - t3 ))

  echo "  [6/6] 校验"
  rows2=$(docker exec "$PG_DST" psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "select (select count(*) from documents), (select count(*) from sessions), (select count(*) from request_traces)" | tr -d '[:space:]')
  rev=$(pgq "$PG_DST" "select version_num from alembic_version")
  ok="一致"; [[ "$rows" == "$rows2" ]] || ok="**不一致**（源=$rows 目标=$rows2）"
  echo "        行数 $ok；alembic=$rev"
  echo
  echo "  RTO(PG) = ${t_restore}s；备份 ${t_backup}s；吞吐 ≈ $(( dump_mb / (t_restore > 0 ? t_restore : 1) ))MB/s（按产物体积/恢复秒）"
  {
    echo "### PG（$PG_DOCS 文档 / $PG_SESSIONS 会话 / $PG_TRACES trace）"
    echo
    echo "| 指标 | 值 |"
    echo "|---|---|"
    echo "| 库大小 | $db_size |"
    echo "| 行数 | $rows |"
    echo "| 备份耗时 | ${t_backup}s |"
    echo "| 备份产物 | ${dump_mb}MB（gzip） |"
    echo "| 恢复耗时（RTO） | ${t_restore}s |"
    echo "| 恢复后行数 | $rows2（$ok） |"
    echo
  } >> "$RESULTS"
}

# ---------------------------------------------------------------------------
# Milvus
# ---------------------------------------------------------------------------
milvus_ready() {  # container
  docker exec -i backend-prod python -W ignore - <<PY 2>/dev/null | grep -q READY
from pymilvus import connections, utility
try:
    connections.connect(host="$1", port="19530", timeout=5)
    utility.list_collections()
    print("READY")
except Exception:
    pass
PY
}

start_milvus_stack() {  # prefix etcd minio milvus vol_suffix
  local p="$1" etcd="$2" minio="$3" milvus="$4"
  docker rm -f "$milvus" "$minio" "$etcd" >/dev/null 2>&1 || true
  for v in "dscale_etcd_$p" "dscale_minio_${p}_milvus" "dscale_milvus_$p"; do
    docker volume rm "$v" >/dev/null 2>&1 || true
  done
  docker run -d --name "$etcd" --network "$NET" -v "dscale_etcd_$p:/etcd" quay.io/coreos/etcd:v3.5.30 \
    etcd -advertise-client-urls=http://"$etcd":2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd >/dev/null
  docker run -d --name "$minio" --network "$NET" \
    -e MINIO_ROOT_USER=scale_minio -e MINIO_ROOT_PASSWORD=scale_minio_pw \
    -e MINIO_ROOT_USER_FILE= -e MINIO_ROOT_PASSWORD_FILE= \
    -e MINIO_ACCESS_KEY_FILE= -e MINIO_SECRET_KEY_FILE= \
    -v "dscale_minio_${p}_milvus:/data" pgsty/minio:RELEASE.2026-06-18T00-00-00Z server /data >/dev/null
  # 等 MinIO 真的能连上再建 bucket：只 sleep 5 在目标实例冷启动时不够，
  # 失败时 `set -e` 会直接退出而**一行日志都没有**（第一版就这样静默死在 [4/6]）。
  for _ in $(seq 1 30); do
    docker exec "$minio" mc ready local >/dev/null 2>&1 && break
    sleep 1
  done
  docker exec "$minio" sh -c "mc alias set bk http://127.0.0.1:9000 scale_minio scale_minio_pw >/dev/null && mc mb --ignore-existing bk/$MILVUS_BUCKET" >/dev/null \
    || { echo "MinIO $minio 初始化失败（bucket 未建）" >&2; return 1; }
  docker run -d --name "$milvus" --network "$NET" \
    -e ETCD_ENDPOINTS="$etcd:2379" -e MINIO_ADDRESS="$minio:9000" \
    -e MINIO_ACCESS_KEY_ID=scale_minio -e MINIO_SECRET_ACCESS_KEY=scale_minio_pw \
    -e MINIO_BUCKET_NAME="$MILVUS_BUCKET" \
    -v "dscale_milvus_$p:/var/lib/milvus" milvusdb/milvus:v2.6.17 milvus run standalone >/dev/null
  for _ in $(seq 1 40); do milvus_ready "$milvus" && return 0; sleep 4; done
  echo "Milvus $milvus 未就绪" >&2; docker logs --tail 20 "$milvus" >&2; return 1
}

run_milvus() {
  echo
  echo "=== Milvus 规模演练：$MILVUS_ROWS 条 / $MILVUS_DIM 维（dense + sparse）==="
  docker network create "$NET" >/dev/null 2>&1 || true
  t0=$(now)
  start_milvus_stack src "$MSRC_ETCD" "$MSRC_MINIO" "$MSRC_MILVUS"
  echo "  [1/6] 源实例就绪 $(( $(now) - t0 ))s"

  echo "  [2/6] 灌 $MILVUS_ROWS 条（含稀疏）并建索引"
  t1=$(now)
  ROWS="$MILVUS_ROWS" DIM="$MILVUS_DIM" COLL="$MILVUS_COLL" HOST="$MSRC_MILVUS" \
    docker exec -i -e ROWS -e DIM -e COLL -e HOST backend-prod python -W ignore - <<'PY'
import os, random, time
from pymilvus import connections, utility, Collection, CollectionSchema, FieldSchema, DataType
rows, dim = int(os.environ["ROWS"]), int(os.environ["DIM"])
coll = os.environ["COLL"]
connections.connect(host=os.environ["HOST"], port="19530")
if utility.has_collection(coll):
    utility.drop_collection(coll)
fields = [
    FieldSchema("id", DataType.INT64, is_primary=True, auto_id=False),
    FieldSchema("text", DataType.VARCHAR, max_length=1024),
    FieldSchema("kb_id", DataType.VARCHAR, max_length=64),
    FieldSchema("vec", DataType.FLOAT_VECTOR, dim=dim),
    FieldSchema("sparse", DataType.SPARSE_FLOAT_VECTOR),
]
c = Collection(coll, CollectionSchema(fields), consistency_level="Bounded")
random.seed(7)
batch = 2000
t0 = time.time()
for start in range(0, rows, batch):
    ids = list(range(start, min(start + batch, rows)))
    c.insert([ids,
              [f"chunk {i} " + "内容" * 64 for i in ids],
              ["kb_demo"] * len(ids),
              [[random.random() for _ in range(dim)] for _ in ids],
              [{d: random.random() for d in random.sample(range(60000), 8)} for _ in ids]])
c.flush()
print(f"  insert {time.time()-t0:.1f}s")
t1 = time.time()
c.create_index("vec", {"index_type": "HNSW", "metric_type": "COSINE",
                       "params": {"M": 16, "efConstruction": 128}})
c.create_index("sparse", {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "IP"})
c.load()
print(f"  index+load {time.time()-t1:.1f}s  entities={c.num_entities}")
PY
  echo "        灌数据+建索引耗时 $(( $(now) - t1 ))s"

  echo "  [3/6] 备份"
  mkdir -p "$WORK/mcfg"
  CFG_MILVUS="$MSRC_MILVUS" CFG_MINIO="$MSRC_MINIO" CFG_BUCKET="$BACKUP_BUCKET" \
  CFG_U=scale_minio CFG_P=scale_minio_pw python - <<'PY' > "$WORK/mcfg/backup.yaml"
import os
e = os.environ
print(f"""configVersion: v2
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
    bucketName: "{os.environ.get('MBUCKET', 'a-bucket')}"
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
""", end="")
PY
  docker exec "$MSRC_MINIO" sh -c "mc alias set bk http://127.0.0.1:9000 scale_minio scale_minio_pw >/dev/null && mc mb --ignore-existing bk/$BACKUP_BUCKET" >/dev/null
  t2=$(now)
  docker run --rm --network "$NET" -v "$(winpath "$WORK/mcfg"):/cfg" \
    --entrypoint /app/milvus-backup milvusdb/milvus-backup:latest \
    --config /cfg/backup.yaml create -n "$BACKUP_NAME" --filter "$MILVUS_COLL" \
    > "$WORK/mcfg/create.log" 2>&1 || { tail -20 "$WORK/mcfg/create.log" >&2; return 1; }
  t_backup=$(( $(now) - t2 ))
  # mc du 输出形如 `71MiB\t9 objects`：分开取，别拼成「71MiB9」
  bsize=$(docker exec "$MSRC_MINIO" sh -c \
    "mc alias set bk http://127.0.0.1:9000 scale_minio scale_minio_pw >/dev/null; mc du bk/$BACKUP_BUCKET" 2>/dev/null | tail -1 | awk '{print $1" / "$2" "$3}')
  echo "        备份 ${t_backup}s，体积 ${bsize}"

  echo "  [4/6] 起目标实例"
  t3=$(now)
  start_milvus_stack dst "$MDST_ETCD" "$MDST_MINIO" "$MDST_MILVUS"
  t_start=$(( $(now) - t3 ))
  # 把备份搬到目标 MinIO：**先建目标 bucket 再 mirror**，否则 mc 报
  # 「bucket does not exist」并以非零退出，被 set -e 静默吞掉
  docker exec "$MDST_MINIO" sh -c "mc alias set bk http://127.0.0.1:9000 scale_minio scale_minio_pw >/dev/null && mc mb --ignore-existing bk/$BACKUP_BUCKET" >/dev/null
  docker exec "$MSRC_MINIO" sh -c '
    mc alias set src http://127.0.0.1:9000 scale_minio scale_minio_pw >/dev/null
    mc alias set dst http://'"$MDST_MINIO"':9000 scale_minio scale_minio_pw >/dev/null
    mc mirror --overwrite src/'"$BACKUP_BUCKET"' dst/'"$BACKUP_BUCKET"' >/dev/null
  ' || { echo "把备份搬到目标 MinIO 失败" >&2; return 1; }

  echo "  [5/6] 恢复（--rebuild_index）"
  CFG_MILVUS="$MDST_MILVUS" CFG_MINIO="$MDST_MINIO" CFG_BUCKET="$BACKUP_BUCKET" \
  CFG_U=scale_minio CFG_P=scale_minio_pw MBUCKET="$MILVUS_BUCKET" python - <<'PY' > "$WORK/mcfg/restore.yaml"
import os
e = os.environ
print(f"""configVersion: v2
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
    bucketName: "{e['MBUCKET']}"
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
""", end="")
PY
  t4=$(now)
  docker run --rm --network "$NET" -v "$(winpath "$WORK/mcfg"):/cfg" \
    --entrypoint /app/milvus-backup milvusdb/milvus-backup:latest \
    --config /cfg/restore.yaml restore -n "$BACKUP_NAME" --rebuild_index \
    > "$WORK/mcfg/restore.log" 2>&1 || { tail -20 "$WORK/mcfg/restore.log" >&2; return 1; }
  t_restore=$(( $(now) - t4 ))

  echo "  [6/6] 校验"
  HOST="$MDST_MILVUS" COLL="$MILVUS_COLL" docker exec -i -e HOST -e COLL backend-prod python -W ignore - <<'PY'
import os
from pymilvus import connections, Collection
connections.connect(host=os.environ["HOST"], port="19530")
c = Collection(os.environ["COLL"])
c.load()
print(f"        目标 entities={c.num_entities} indexes=" +
      ", ".join(f"{i.field_name}:{i.params.get('index_type')}" for i in c.indexes))
PY

  total=$(( t_start + t_restore ))
  echo
  echo "  RTO(Milvus) = ${total}s（起实例 ${t_start}s + 恢复 ${t_restore}s）；备份 ${t_backup}s"
  {
    echo "### Milvus（$MILVUS_ROWS 条 / $MILVUS_DIM 维，dense+sparse）"
    echo
    echo "| 指标 | 值 |"
    echo "|---|---|"
    echo "| 备份耗时 | ${t_backup}s |"
    echo "| 备份体积 | ${bsize} |"
    echo "| 目标实例启动 | ${t_start}s |"
    echo "| 恢复（含重建双索引） | ${t_restore}s |"
    echo "| RTO | **${total}s** |"
    echo
  } >> "$RESULTS"
}

# ---------------------------------------------------------------------------
# MinIO
# ---------------------------------------------------------------------------
run_minio() {
  echo
  echo "=== MinIO 规模演练：$MINIO_OBJECTS 个对象 × ${MINIO_SIZE_KB}KB ==="
  docker network create "$NET" >/dev/null 2>&1 || true
  # 不删旧目录：删 1000 个文件会撞「批量删除需确认」的策略而静默退出（第一次跑就死在这），
  # 而种子是同名覆盖写，重跑本来就不需要先清空。
  src_dir="$WORK/minio_src"; mkdir -p "$src_dir/a" "$src_dir/b/c"
  echo "  [1/5] 本地生成对象"
  t0=$(now)
  for i in $(seq 1 "$MINIO_OBJECTS"); do
    head -c $(( MINIO_SIZE_KB * 1024 )) /dev/urandom > "$src_dir/$( [[ $((i % 3)) == 0 ]] && echo b/c || echo a )/obj_$i.bin"
  done
  total_mb=$(( MINIO_OBJECTS * MINIO_SIZE_KB / 1024 ))
  echo "        $MINIO_OBJECTS 个 / ${total_mb}MB，耗时 $(( $(now) - t0 ))s"

  echo "  [2/5] 起源 MinIO 并灌入"
  docker rm -f "$MINIO_SRC" >/dev/null 2>&1 || true
  docker volume rm dscale_minio_src >/dev/null 2>&1 || true
  docker run -d --name "$MINIO_SRC" --network "$NET" -p "127.0.0.1:$MINIO_SRC_PORT:9000" \
    -e MINIO_ROOT_USER=scale_minio -e MINIO_ROOT_PASSWORD=scale_minio_pw \
    -e MINIO_ROOT_USER_FILE= -e MINIO_ROOT_PASSWORD_FILE= \
    -e MINIO_ACCESS_KEY_FILE= -e MINIO_SECRET_KEY_FILE= \
    -v dscale_minio_src:/data pgsty/minio:RELEASE.2026-06-18T00-00-00Z server /data >/dev/null
  sleep 5
  docker exec "$MINIO_SRC" sh -c "mc alias set l http://127.0.0.1:9000 scale_minio scale_minio_pw >/dev/null && mc mb --ignore-existing l/documents" >/dev/null
  t1=$(now)
  docker cp "$(winpath "$src_dir")/." "$MINIO_SRC:/tmp/seed/"
  docker exec "$MINIO_SRC" sh -c "mc mirror --overwrite /tmp/seed l/documents >/dev/null && rm -rf /tmp/seed"
  echo "        灌入耗时 $(( $(now) - t1 ))s"

  echo "  [3/5] 备份（mc mirror 到宿主机）"
  bdir="$WORK/minio_backup_$(date +%H%M%S)"; mkdir -p "$bdir"
  t2=$(now)
  docker exec "$MINIO_SRC" sh -c "rm -rf /tmp/bk && mkdir -p /tmp/bk && mc mirror --overwrite l/documents /tmp/bk" >/dev/null
  docker cp "$MINIO_SRC:/tmp/bk/." "$(winpath "$bdir")/"
  t_backup=$(( $(now) - t2 ))
  n_bk=$(find "$bdir" -type f | wc -l | tr -d '[:space:]')
  bk_mb=$(du -sm "$bdir" | cut -f1)
  echo "        ${t_backup}s，${n_bk} 个文件 / ${bk_mb}MB"

  echo "  [4/5] 恢复到全新实例"
  docker rm -f "$MINIO_DST" >/dev/null 2>&1 || true
  docker volume rm dscale_minio_dst >/dev/null 2>&1 || true
  t3=$(now)
  docker run -d --name "$MINIO_DST" --network "$NET" -p "127.0.0.1:$MINIO_DST_PORT:9000" \
    -e MINIO_ROOT_USER=scale_minio -e MINIO_ROOT_PASSWORD=scale_minio_pw \
    -e MINIO_ROOT_USER_FILE= -e MINIO_ROOT_PASSWORD_FILE= \
    -e MINIO_ACCESS_KEY_FILE= -e MINIO_SECRET_KEY_FILE= \
    -v dscale_minio_dst:/data pgsty/minio:RELEASE.2026-06-18T00-00-00Z server /data >/dev/null
  for _ in $(seq 1 30); do
    docker exec "$MINIO_DST" mc ready local >/dev/null 2>&1 && break
    sleep 1
  done
  t4=$(now)
  # 这里直接用它自己的 mc 回灌，而不是调 restore.sh：restore.sh 的容器分支从
  # /run/secrets 取生产凭据，而演练实例用的是另一套（换凭据就等于改被测对象）。
  # 执行的动作与 restore.sh 完全相同（mc mb + mc mirror），量的是同一件事。
  docker exec "$MINIO_DST" sh -c "mc alias set bklocal http://127.0.0.1:9000 scale_minio scale_minio_pw >/dev/null" >/dev/null
  docker cp "$(winpath "$bdir")/." "$MINIO_DST:/tmp/restore_src/"
  docker exec "$MINIO_DST" sh -c "
    mc mb --ignore-existing bklocal/documents >/dev/null
    mc mirror --overwrite /tmp/restore_src bklocal/documents >/dev/null
    rm -rf /tmp/restore_src" || { echo "回灌失败" >&2; return 1; }
  t_restore=$(( $(now) - t4 ))

  echo "  [5/5] 校验（对象数 / 内容哈希集合）"
  n_dst=$(docker exec "$MINIO_DST" sh -c "mc ls -r bklocal/documents" | wc -l | tr -d '[:space:]')
  # 不用 xargs：MSYS 的 xargs 在处理大量路径时会崩（之前 MinIO 演练踩过），用 -exec ... +
  # 哈希必须回宿主机算：minio 镜像里没有 sha256sum，在容器里算得到的是空输入，
  # 两侧一起退化成 e3b0c442（空串的 sha256）→ 看起来「一致」其实什么都没比。
  chk="$WORK/minio_check"; mkdir -p "$chk"
  docker exec "$MINIO_DST" sh -c "rm -rf /tmp/chk && mc mirror --overwrite bklocal/documents /tmp/chk" >/dev/null
  docker cp "$MINIO_DST:/tmp/chk/." "$(winpath "$chk")/" >/dev/null
src_list=$(cd "$src_dir" && find . -type f -exec sha256sum {} + | awk '{print $1}' | sort | sha256sum | cut -c1-16)
  dst_list=$(cd "$chk" && find . -type f -exec sha256sum {} + | awk '{print $1}' | sort | sha256sum | cut -c1-16)
  empty_hash=$(printf '' | sha256sum | cut -c1-16)
  if [[ -z "$src_list" || "$src_list" == "$empty_hash" || "$dst_list" == "$empty_hash" ]]; then
    echo "        校验无效：哈希退化成空串（$src_list / $dst_list），本次结论不成立" >&2
    return 1
  fi
  if [[ "$n_bk" != "$n_dst" ]]; then
    echo "      ✗ 对象数不一致（源 $n_bk / 目标 $n_dst）" >&2; return 1
  fi
  if [[ "$src_list" != "$dst_list" ]]; then
    echo "      ✗ 内容哈希集合不一致：备份没有完整往返" >&2; return 1
  fi
  echo "      ✓ 对象数一致（$n_dst），内容哈希集合一致（$dst_list）"
  echo "        源 ${n_bk} 个 / 目标 ${n_dst} 个；哈希集合 源=$src_list 目标=$dst_list"
  total=$(( $(now) - t3 ))
  echo
  echo "  RTO(MinIO) = ${total}s（起实例 $(( t4 - t3 ))s + 回灌 ${t_restore}s）；备份 ${t_backup}s"
  {
    echo "### MinIO（$MINIO_OBJECTS 对象 / ${total_mb}MB）"
    echo
    echo "| 指标 | 值 |"
    echo "|---|---|"
    echo "| 备份耗时 | ${t_backup}s |"
    echo "| 备份体积 | ${bk_mb}MB |"
    echo "| 实例启动 | $(( t4 - t3 ))s |"
    echo "| 回灌 | ${t_restore}s |"
    echo "| RTO | **${total}s** |"
    echo "| 校验 | 源 ${n_bk} 个 / 目标 ${n_dst} 个；哈希集合 ${src_list} vs ${dst_list} |"
    echo
  } >> "$RESULTS"
}

case "$MODE" in
  pg) run_pg ;;
  milvus) run_milvus ;;
  minio) run_minio ;;
  all) run_pg; run_milvus; run_minio ;;
  *) echo "MODE 只能是 pg / milvus / minio / all" >&2; exit 1 ;;
esac

echo
echo "== 汇总（已写入 $RESULTS）=="
cat "$RESULTS"
