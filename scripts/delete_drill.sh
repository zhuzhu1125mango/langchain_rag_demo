#!/usr/bin/env bash
# P0-2：删除链端到端演练——证明「删完之后四处真的没有残留」。
#
# 为什么需要（2026-11-07 对抗性检查 P0-2）：
#   backend/tests 里 13 例删除测试用的是 fake store，只能证明「删除顺序」与
#   「某一步失败不回退」。它们不证明 Milvus 向量、MinIO 对象、Redis 语义缓存
#   真的被清掉——而这三处恰好是「删了还能被检索到 / 还能被命中」的来源。
#
# 做法：自建自删。建临时 KB → 传一个带唯一标记的小 txt → 等处理完成 →
# 记下「删除前确实有东西」的基线 → 删文档 → 删 KB → 断言四处回到零。
#
# 退出码（刻意区分「跑过且干净」和「没跑」）：
#   0 = 四处断言全部通过
#   1 = 有残留（会打印残留清单）
#   3 = 跳过：环境不具备（staging 栈没起、Ollama 模型缺失、注册关闭…）
#       **不允许**把跳过当成功——「看起来接了其实永远 skip 的绿灯」比没有更糟。
#
# 默认跑开发栈（写操作不该打生产）。要换目标：
#   API_BASE=http://127.0.0.1:8001/api BACKEND=backend-prod ./scripts/delete_drill.sh
#
# 自测断言有效性（证明它不是恒真）：
#   INJECT_RESIDUAL=redis ./scripts/delete_drill.sh    # 期望 exit 1，报 Redis 残留
#   INJECT_RESIDUAL=minio ./scripts/delete_drill.sh    # 期望 exit 1，报 MinIO 残留
# 注入物会在退出前清掉，不影响下一次演练。
#
# 坑位记录（改脚本前先读）：
#  1) Milvus 残留判定必须用 count(*)（query + output_fields=["count(*)"]），
#     不能用 num_entities——后者含软删除标记，会把「已逻辑删除」误判成仍有残留。
#  2) 上传要 Ollama 做 embedding。不可用时明确 exit 3，不静默成功。
#  3) Git Bash 下原生 curl 不认 /c/... 路径：所有 -o 目标都要 winpath 转换，
#     否则「写不进去但返回 200」这种假成功会一路带到断言里。
set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
TMP_DIR="$ROOT_DIR/backups/drill-delete-$STAMP"

API_BASE="${API_BASE:-http://127.0.0.1:8000/api}"
BACKEND="${BACKEND:-backend-dev}"
PG_CONTAINER="${PG_CONTAINER:-postgres-dev}"
MILVUS_CONTAINER="${MILVUS_CONTAINER:-milvus-standalone-dev}"
MINIO_CONTAINER="${MINIO_CONTAINER:-minio-dev}"
REDIS_CONTAINER="${REDIS_CONTAINER:-redis-dev}"

# 固定演练账号：每次跑都复用它，避免 users 表里堆一串 delete_drill_<时间戳>。
DRILL_USER="${DRILL_USER:-delete_drill}"
DRILL_PASS="${DRILL_PASS:-DeleteDrill_2026!}"

MARK="drill-${STAMP}"
KB_NAME="drill-kb-${STAMP}"
UPLOAD_TIMEOUT="${UPLOAD_TIMEOUT:-240}"
DELETE_TIMEOUT="${DELETE_TIMEOUT:-90}"

KB_ID=""
DOC_ID=""
TOKEN=""
USER_ID=""
CACHE_VERIFIED=0
FAILED=0

have() { command -v "$1" >/dev/null 2>&1; }

# 原生 curl 是 Windows 程序：-o /c/... 会被当成相对路径丢掉。
winpath() {
  if have cygpath; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

step() { echo; echo "=== $* ==="; }
ok()   { echo "  [ok]   $*"; }
bad()  { FAILED=1; echo "  [FAIL] $*"; }
skip() { echo; echo "[跳过] $*"; echo "[跳过] 这是「没跑」，不是「跑过且干净」。"; exit 3; }

cleanup() {
  local rc=$?
  if [[ -n "$KB_ID" && -n "$TOKEN" ]]; then
    # 残留的 KB 由这里兜底删掉；断言已经跑完，不会掩盖结论。
    curl -s -o "$(winpath /dev/null)" -X DELETE \
      -H "Authorization: Bearer $TOKEN" "$API_BASE/knowledge_bases/$KB_ID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
  return $rc
}
trap cleanup EXIT

# 通过 backend 容器执行一段 python：它已经装好 sqlalchemy/pymilvus/minio/redis，
# 并且配置指向同一套 PG/Milvus/MinIO/Redis，不必在宿主机再拼一套连接参数。
py() { docker exec -i "$BACKEND" python -W ignore -; }

# 探针：一次取回四处存储在该 KB / 文档上的实际条数。
# 只读，不修改任何状态。参数走 -e 传进容器，避免 heredoc 引号拼接。
probe() {
  # tail -1：容器里的 logger 会往 stdout 打启动信息，JSON 是最后一行
  docker exec -i -e PROBE_USER="$USER_ID" -e PROBE_KB="$KB_ID" -e PROBE_DOC="$DOC_ID" \
    "$BACKEND" python -W ignore - <<'PY' | tail -1
import asyncio, json, os
from sqlalchemy import text

USER = os.environ.get("PROBE_USER", "")
KB = os.environ.get("PROBE_KB", "")
DOC = os.environ.get("PROBE_DOC", "")

async def main():
    out = {}

    # 1) PostgreSQL：文档行与知识库行
    from src.database import async_session_maker
    async with async_session_maker() as db:
        out["pg_documents"] = int((await db.execute(
            text("select count(*) from documents where kb_id = :kb"), {"kb": KB}
        )).scalar() or 0)
        out["pg_kb"] = int((await db.execute(
            text("select count(*) from knowledge_bases where id = :kb"), {"kb": KB}
        )).scalar() or 0)

    # 2) Milvus：必须 count(*)，不能用 num_entities（含软删标记）
    from src.config import settings
    from pymilvus import connections, Collection
    import uuid
    alias = "probe_" + uuid.uuid4().hex[:8]
    out["milvus"] = "unavailable"
    try:
        connections.connect(
            alias=alias,
            host=settings.milvus.MILVUS_HOST,
            port=str(settings.milvus.MILVUS_PORT),
            timeout=5,
        )
        col = Collection(settings.milvus.MILVUS_COLLECTION_NAME, using=alias)
        col.load()
        def c(expr):
            rows = col.query(expr=expr, output_fields=["count(*)"], using=alias)
            return int(rows[0]["count(*)"] if rows else 0)
        out["milvus_doc"] = c(f"document_id == '{DOC}'") if DOC else 0
        out["milvus_kb"] = c(f"kb_id == '{KB}'") if KB else 0
        out["milvus"] = "ok"
    except Exception as exc:
        out["milvus_error"] = str(exc)[:200]

    # 3) MinIO：该文档前缀下的对象
    out["minio"] = "unavailable"
    try:
        from src.services.minio_service import MinioService
        svc = await MinioService.get_instance()
        client = getattr(svc, "_client", None) or getattr(svc, "client", None)
        bucket = settings.minio.MINIO_BUCKET_NAME
        prefix = f"documents/{DOC}/" if DOC else ""
        objs = list(client.list_objects(bucket, prefix=prefix, recursive=True)) if prefix else []
        out["minio_objects"] = len(objs)
        out["minio"] = "ok"
    except Exception as exc:
        out["minio_error"] = str(exc)[:200]

    # 4) Redis：该用户的语义缓存 scope + 该 KB 的反向索引
    out["redis"] = "unavailable"
    try:
        from src.services.semantic_cache_service import SemanticCacheService
        sc = SemanticCacheService()
        cache = await sc._get_cache()
        keys, members = [], []
        if getattr(cache, "available", False):
            raw = await cache.client.keys(f"rag:semcache:{USER}:*")
            keys = [k.decode() if isinstance(k, bytes) else k for k in raw]
            raw2 = await cache.client.smembers(f"rag:semcache:index:kb:{KB}")
            members = [m.decode() if isinstance(m, bytes) else m for m in (raw2 or [])]
        out["redis_scope_keys"] = keys
        out["redis_index_members"] = members
        out["redis"] = "ok"
    except Exception as exc:
        out["redis_error"] = str(exc)[:200]

    print(json.dumps(out, ensure_ascii=False))

asyncio.run(main())
PY
}

# 从 JSON 里取一个字段（探针输出只有一行）。解析失败返回空串，不吐 traceback。
jget() {
  printf '%s' "$1" | "$PYTHON_BIN" -c "import json,sys
try: print(json.load(sys.stdin).get('$2',''))
except Exception: print('')" 2>/dev/null
}

PYTHON_BIN="$(command -v python || command -v python3 || true)"

step "0/8 前置检查"
[[ -n "$PYTHON_BIN" ]] || skip "宿主机没有 python：脚本用它在 bash 与 JSON 之间取值"

missing=()
for c in "$BACKEND" "$PG_CONTAINER" "$MILVUS_CONTAINER" "$MINIO_CONTAINER" "$REDIS_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || missing+=("$c")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  skip "缺少容器：${missing[*]}。开发栈未启动，先跑 ./scripts/start-dev.sh"
fi

# backend 就绪（冷启动要加载 embedding 模型，给足时间）
ready=0
for _ in $(seq 1 30); do
  if docker exec "$BACKEND" curl -sf -o /dev/null http://127.0.0.1:8000/health/ready 2>/dev/null; then
    ready=1; break
  fi
  sleep 2
done
[[ "$ready" == "1" ]] || skip "$BACKEND 未就绪（/health/ready 不通）"
ok "$BACKEND 就绪"

# Ollama：上传要靠它做 embedding。探测不到就明确跳过，而不是等上传超时。
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.dev}"
_env_value() {
  [[ -f "$ENV_FILE" ]] || return 0
  grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- \
    | sed 's/[[:space:]]*#.*$//' | tr -d '"' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}
EMBED_MODEL="${EMBED_MODEL:-$(_env_value EMBEDDING_MODEL_NAME)}"
EMBED_MODEL="${EMBED_MODEL:-bge-m3}"

if ! have ollama; then
  skip "宿主机没有 ollama 命令（上传需要 embedding），无法验证删除链"
fi
if ! tags="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')"; then
  skip "读取 ollama 模型列表失败（服务未启动？）"
fi
base="${EMBED_MODEL%%:*}"
if ! grep -qx -e "$EMBED_MODEL" -e "$base" -e "$base:latest" <<< "$tags"; then
  skip "Ollama 缺少 embedding 模型 ${EMBED_MODEL}（ollama pull ${EMBED_MODEL}）"
fi
ok "Ollama 可用，embedding 模型 ${EMBED_MODEL}"

mkdir -p "$TMP_DIR"

step "1/8 取得演练账号（固定账号，先登录，不存在再注册）"
auth_body="{\"username\":\"$DRILL_USER\",\"password\":\"$DRILL_PASS\"}"
resp="$(curl -s -X POST "$API_BASE/auth/login" -H 'Content-Type: application/json' -d "$auth_body" || true)"
TOKEN="$(printf '%s' "$resp" | "$PYTHON_BIN" -c 'import json,sys
try: print(json.load(sys.stdin).get("access_token",""))
except Exception: print("")')"

if [[ -z "$TOKEN" ]]; then
  echo "  登录失败，尝试注册 $DRILL_USER"
  resp="$(curl -s -X POST "$API_BASE/auth/register" -H 'Content-Type: application/json' -d "$auth_body" || true)"
  TOKEN="$(printf '%s' "$resp" | "$PYTHON_BIN" -c 'import json,sys
try: print(json.load(sys.stdin).get("access_token",""))
except Exception: print("")')"
fi
[[ -n "$TOKEN" ]] || skip "既登录不上也注册不了（AUTH_ALLOW_REGISTRATION 关闭且账号不存在）"
ok "token ${#TOKEN} 字符"

USER_ID="$(printf '%s' "$resp" | "$PYTHON_BIN" -c 'import json,sys
try:
    d=json.load(sys.stdin); print(d.get("user",{}).get("id","") or d.get("user_id",""))
except Exception: print("")')"
if [[ -z "$USER_ID" ]]; then
  # register/login 的响应结构可能只给 token；从库里按用户名反查，避免猜字段名
  USER_ID="$(docker exec "$PG_CONTAINER" psql -U "$(docker exec "$PG_CONTAINER" printenv POSTGRES_USER)" \
    -d "$(docker exec "$PG_CONTAINER" printenv POSTGRES_DB)" -tAc \
    "select id from users where username='$DRILL_USER'" | tr -d '[:space:]')"
fi
[[ -n "$USER_ID" ]] || skip "拿不到演练账号的 user_id"
ok "user_id=$USER_ID"

step "2/8 建临时知识库（$KB_NAME）"
resp="$(curl -s -X POST "$API_BASE/knowledge_bases/" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"$KB_NAME\",\"description\":\"delete drill $STAMP\"}")"
KB_ID="$(printf '%s' "$resp" | "$PYTHON_BIN" -c 'import json,sys
try: print(json.load(sys.stdin).get("id",""))
except Exception: print("")')"
[[ -n "$KB_ID" ]] || { echo "  响应：$resp" >&2; skip "建知识库失败（响应里没有 id）"; }
ok "kb_id=$KB_ID"

step "3/8 上传带唯一标记的 txt 并等待处理完成"
# 内容带唯一标记：万一有残留，能在 Milvus/MinIO 里一眼认出来是这个脚本留下的。
cat > "$TMP_DIR/$MARK.txt" <<EOF
delete drill marker: $MARK
本文件仅用于验证删除链，脚本结束时会连同知识库一起删除。
$(for i in $(seq 1 30); do echo "填充行 $i：$MARK 这是一段用于产生多个切片的中文内容，确保向量与切片数量不为零。"; done)
EOF

resp="$(curl -s -X POST "$API_BASE/documents/upload?kb_id=$KB_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@$(winpath "$TMP_DIR/$MARK.txt");type=text/plain")"
DOC_ID="$(printf '%s' "$resp" | "$PYTHON_BIN" -c 'import json,sys
try: print(json.load(sys.stdin).get("id",""))
except Exception: print("")')"
[[ -n "$DOC_ID" ]] || { echo "  响应：$resp" >&2; bad "上传失败"; exit 1; }
ok "doc_id=$DOC_ID"

echo "  等待处理完成（最多 ${UPLOAD_TIMEOUT}s）…"
status=""
chunks=0
for _ in $(seq 1 $((UPLOAD_TIMEOUT / 3))); do
  resp="$(curl -s "$API_BASE/documents/?kb_id=$KB_ID" -H "Authorization: Bearer $TOKEN" || true)"
  read -r status chunks <<<"$(printf '%s' "$resp" | "$PYTHON_BIN" -c '
import json,sys
try:
    rows = json.load(sys.stdin)
    row = rows[0] if rows else {}
    print(row.get("processing_status") or row.get("status") or "", row.get("chunks_count") or 0)
except Exception:
    print("", 0)')" || true
  [[ "$status" == "completed" ]] && break
  if [[ "$status" == "failed" ]]; then
    bad "文档处理失败（processing_status=failed），无法继续验证删除链"
    exit 1
  fi
  sleep 3
done
[[ "$status" == "completed" ]] || { bad "等待处理超时（最后状态=${status:-unknown}）"; exit 1; }
[[ "${chunks:-0}" -gt 0 ]] || { bad "处理完成但 chunks_count=0，后面「删除后为 0」就没有意义"; exit 1; }
ok "处理完成，chunks=$chunks"

step "4/8 写入一条语义缓存（否则第四处断言永远是 0，等于没验）"
cache_out="$(docker exec -i -e DRILL_U="$USER_ID" -e DRILL_KB="$KB_ID" -e DRILL_Q="$MARK 的唯一问题" "$BACKEND" python -W ignore - <<'PY' 2>&1 | tail -1
import asyncio, json, os
from src.services.semantic_cache_service import SemanticCacheService

async def main():
    ok = await SemanticCacheService().store(
        os.environ["DRILL_U"], [os.environ["DRILL_KB"]],
        os.environ["DRILL_Q"], "drill answer", ["drill source"],
        [{"source": "drill", "document_id": "drill"}],
        embedding=[0.123] * 1024,   # 假向量：只为走真实的 key/field/index 写入路径
    )
    print(json.dumps({"stored": bool(ok)}))

asyncio.run(main())
PY
)"
if [[ "$(printf '%s' "$cache_out" | "$PYTHON_BIN" -c 'import json,sys
try: print(json.load(sys.stdin).get("stored"))
except Exception: print("parse-error")' 2>/dev/null)" == "True" ]]; then
  CACHE_VERIFIED=1
  ok "语义缓存已写入"
else
  bad "语义缓存写入失败（SEMANTIC_CACHE_ENABLED=false 或 Redis 不可用）：第四处无法验证"
  echo "     服务返回：$cache_out"
fi

step "5/8 基线快照：确认删除前四处确实都有东西"
BEFORE="$(probe)"
echo "  $BEFORE"

[[ "$(jget "$BEFORE" milvus)" == "ok" ]]   || bad "Milvus 探针不可用：$(jget "$BEFORE" milvus_error)"
[[ "$(jget "$BEFORE" minio)" == "ok" ]]    || bad "MinIO 探针不可用：$(jget "$BEFORE" minio_error)"
[[ "$(jget "$BEFORE" redis)" == "ok" ]]    || bad "Redis 探针不可用：$(jget "$BEFORE" redis_error)"

[[ "$(jget "$BEFORE" pg_documents)" == "1" ]] || bad "基线异常：documents 行数应为 1，实际 $(jget "$BEFORE" pg_documents)"
before_milvus="$(jget "$BEFORE" milvus_doc)"
if ! [[ "$before_milvus" =~ ^[0-9]+$ ]] || [[ "$before_milvus" -le 0 ]]; then
  bad "基线异常：Milvus 里该文档向量为 ${before_milvus:-空}（上传没进去？）"
fi
[[ "$(jget "$BEFORE" minio_objects)" == "1" ]] || bad "基线异常：MinIO 该文档对象数应为 1，实际 $(jget "$BEFORE" minio_objects)"
if [[ "$CACHE_VERIFIED" == "1" ]]; then
  [[ "$(jget "$BEFORE" redis_scope_keys)" == *"rag:semcache:"* ]] || bad "基线异常：写完缓存却读不到 scope key"
fi
[[ "$FAILED" == "0" ]] || { echo; echo "基线不成立，后续断言没有意义，直接失败。"; exit 1; }
ok "基线成立：documents=1 / milvus=$(jget "$BEFORE" milvus_doc) / minio=1 / redis scope=$(jget "$BEFORE" redis_scope_keys)"

step "6/8 删除文档并等待后台任务完成"
code="$(curl -s -o "$(winpath /dev/null)" -w '%{http_code}' -X DELETE \
  "$API_BASE/documents/$DOC_ID" -H "Authorization: Bearer $TOKEN")"
[[ "$code" == "200" ]] || bad "删除文档返回 HTTP $code"
ok "DELETE /documents/$DOC_ID -> $code"

for _ in $(seq 1 $((DELETE_TIMEOUT / 2))); do
  NOW="$(probe)"
  [[ "$(jget "$NOW" pg_documents)" == "0" && "$(jget "$NOW" milvus_doc)" == "0" ]] && break
  sleep 2
done
ok "文档已从 documents 与 Milvus 消失（pg=$(jget "$NOW" pg_documents), milvus_doc=$(jget "$NOW" milvus_doc)）"

step "7/8 删除知识库（清空级联）"
code="$(curl -s -o "$(winpath /dev/null)" -w '%{http_code}' -X DELETE \
  "$API_BASE/knowledge_bases/$KB_ID" -H "Authorization: Bearer $TOKEN")"
[[ "$code" == "200" ]] || bad "删除知识库返回 HTTP $code"
ok "DELETE /knowledge_bases/$KB_ID -> $code"

# 语义缓存失效是异步队列，给它一点时间；轮询而不是固定 sleep。
for _ in $(seq 1 15); do
  AFTER="$(probe)"
  [[ "$(jget "$AFTER" redis_scope_keys)" == "[]" || -z "$(jget "$AFTER" redis_scope_keys)" ]] && break
  sleep 2
done

# 自测用：故意留一处残留，验证下面的断言不是「恒真」。
# 正式演练不要设置；用来回答「这个脚本真能抓到残留吗」。
if [[ "${INJECT_RESIDUAL:-none}" != "none" ]]; then
  step "7.5/8 注入残留（INJECT_RESIDUAL=$INJECT_RESIDUAL，仅用于自测断言有效性）"
  case "$INJECT_RESIDUAL" in
    redis)
      docker exec -i -e D_U="$USER_ID" -e D_KB="$KB_ID" "$BACKEND" python -W ignore - <<'PY' >/dev/null
import asyncio, os, time
from src.services.semantic_cache_service import SemanticCacheService, _encode_entry

async def main():
    sc = SemanticCacheService()
    cache = await sc._get_cache()
    key = sc.scope_key(os.environ["D_U"], [os.environ["D_KB"]])
    entry = {"question": "injected", "question_norm": "injected", "embedding": [0.1] * 8,
             "answer": "x", "source_texts": [], "source_metadata": [],
             "kb_ids": [os.environ["D_KB"]], "answer_type": "drill", "created_at": time.time()}
    await cache.client.hset(key, "exact:injected", _encode_entry(entry))
    await cache.client.sadd(f"rag:semcache:index:kb:{os.environ['D_KB']}", key)

asyncio.run(main())
PY
      ;;
    minio)
      docker exec -i -e D_DOC="$DOC_ID" "$BACKEND" python -W ignore - <<'PY' >/dev/null
import asyncio, io, os
from src.config import settings
from src.services.minio_service import MinioService

async def main():
    svc = await MinioService.get_instance()
    client = getattr(svc, "_client", None) or getattr(svc, "client", None)
    body = b"injected residual"
    client.put_object(settings.minio.MINIO_BUCKET_NAME,
                      f"documents/{os.environ['D_DOC']}/injected.txt", io.BytesIO(body), len(body))

asyncio.run(main())
PY
      ;;
    *) echo "  未知注入类型：$INJECT_RESIDUAL（支持 redis/minio）" ;;
  esac
  ok "已注入 $INJECT_RESIDUAL 残留，接下来断言应当失败"
fi

step "8/8 断言四处无残留"
AFTER="$(probe)"
echo "  $AFTER"

assert_zero() { # 名称 实际值
  if [[ "$2" == "0" || -z "$2" ]]; then ok "$1 = 0"; else bad "$1 = $2（应为 0）"; fi
}

assert_zero "PostgreSQL documents(kb)" "$(jget "$AFTER" pg_documents)"
assert_zero "PostgreSQL knowledge_bases" "$(jget "$AFTER" pg_kb)"
assert_zero "Milvus 向量(document_id)" "$(jget "$AFTER" milvus_doc)"
assert_zero "Milvus 向量(kb_id)" "$(jget "$AFTER" milvus_kb)"
assert_zero "MinIO 对象(documents/$DOC_ID)" "$(jget "$AFTER" minio_objects)"

if [[ "$CACHE_VERIFIED" == "1" ]]; then
  assert_zero "Redis 语义缓存 scope key" "$(printf '%s' "$AFTER" | "$PYTHON_BIN" -c 'import json,sys
print(len(json.load(sys.stdin).get("redis_scope_keys") or []))')"
  assert_zero "Redis kb→scope 反向索引" "$(printf '%s' "$AFTER" | "$PYTHON_BIN" -c 'import json,sys
print(len(json.load(sys.stdin).get("redis_index_members") or []))')"
else
  echo "  [未测] Redis 语义缓存：写入失败（见 4/8），本次不把这一处算作已验证"
fi

if [[ "$FAILED" != "0" ]]; then
  echo
  echo "有残留。定位用："
  echo "  文档: doc_id=$DOC_ID kb_id=$KB_ID user_id=$USER_ID"
  echo "  Milvus: document_id == '$DOC_ID' / kb_id == '$KB_ID'"
  echo "  MinIO : documents/$DOC_ID/"
  echo "  Redis : rag:semcache:$USER_ID:* 与 rag:semcache:index:kb:$KB_ID"
  if [[ "${INJECT_RESIDUAL:-none}" != "none" ]]; then
    # 自测注入的东西必须清掉，否则下一次演练的基线会被它污染
    docker exec -i -e D_U="$USER_ID" -e D_KB="$KB_ID" -e D_DOC="$DOC_ID" "$BACKEND" python -W ignore - <<'PY' >/dev/null 2>&1 || true
import asyncio, os
from src.config import settings
from src.services.semantic_cache_service import SemanticCacheService
from src.services.minio_service import MinioService

async def main():
    sc = SemanticCacheService()
    cache = await sc._get_cache()
    key = sc.scope_key(os.environ["D_U"], [os.environ["D_KB"]])
    try:
        await cache.client.delete(key)
        await cache.client.srem(f"rag:semcache:index:kb:{os.environ['D_KB']}", key)
    except Exception:
        pass
    try:
        svc = await MinioService.get_instance()
        client = getattr(svc, "_client", None) or getattr(svc, "client", None)
        client.remove_object(settings.minio.MINIO_BUCKET_NAME,
                             f"documents/{os.environ['D_DOC']}/injected.txt")
    except Exception:
        pass

asyncio.run(main())
PY
    echo "（已清理 INJECT_RESIDUAL 注入物）"
  fi
  exit 1
fi

echo
echo "四处全部无残留：PostgreSQL / Milvus / MinIO / Redis$( [[ "$CACHE_VERIFIED" == "1" ]] || echo '（Redis 未测）')"
echo "标记：$MARK（脚本结束时已清理临时 KB 与本地文件）"
