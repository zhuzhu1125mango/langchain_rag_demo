#!/usr/bin/env bash
# P1-3：故障注入演练——证明「错误可判定」不只存在于单测里。
#
# 背景：生产日志里 `severity=` 一次都没出现过，而「失败可判定」是项目的二级卖点。
# 没有一次真实事故验证过它，就等于没实现。
#
# 三类注入，全部打在**专用后端 fault-backend**（见 docker-compose.fault.yml）上，
# 它连的是开发栈的 PG/Milvus/MinIO/Redis；不碰生产，也不污染 backend-dev。
#
#   A 依赖不可达：pause Milvus → 检索必须报 reportable，且**不能**伪装成「没有相关内容」
#   B 打分失败：指向不存在的 reranker → 连续失败必须升级为 reportable，且分数不能变 0
#   C 代码 bug：临时注入未捕获异常 → 500 + request_id + severity=programming + 堆栈
#
# 每类注入后都要撤销并再问一次——证明演练没把服务弄坏，而不是「一次性的牺牲」。
#
# 退出码：0 三类均符合预期；1 有不符合；3 环境不具备（**没跑**，不算通过）
#
# 用法：
#   ./scripts/fault_drill.sh
#   KEEP=1 ./scripts/fault_drill.sh     # 失败时保留 fault-backend 便于查日志
set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$ROOT_DIR/backups/drill-fault-$(date +%Y%m%d_%H%M%S)"

API_BASE="${API_BASE:-http://127.0.0.1:18001/api}"
BACKEND="${BACKEND:-fault-backend}"
MILVUS_CONTAINER="${MILVUS_CONTAINER:-milvus-standalone-dev}"
COMPOSE_BASE="$ROOT_DIR/docker-compose.fault.yml"
COMPOSE_ENV="$ROOT_DIR/.env.dev"

DRILL_USER="${DRILL_USER:-delete_drill}"
DRILL_PASS="${DRILL_PASS:-DeleteDrill_2026!}"
QA_QUESTION="${QA_QUESTION:-这份故障演练文档写了什么？}"
# 等日志落盘的最长时间（秒）：docker logs 的写入有几毫秒到几百毫秒延迟，
# 但绝不该等成「等到出现为止」——超时仍然算失败。
LOG_WAIT="${LOG_WAIT:-20}"

TOKEN=""
KB_ID=""
DOC_ID=""
FAILED=0
# 代码 bug 注入的现场：cleanup 必须无条件还原，哪怕脚本被 Ctrl-C 或超时杀掉
STREAM_TARGET="$ROOT_DIR/backend/src/api/chat/stream.py"
STREAM_BAK=""
PYTHON_BIN="$(command -v python || command -v python3 || true)"

have() { command -v "$1" >/dev/null 2>&1; }
step() { echo; echo "=== $* ==="; }
ok()   { echo "  [ok]   $*"; }
bad()  { FAILED=1; echo "  [FAIL] $*"; }
skip() { echo; echo "[跳过] $*"; echo "[跳过] 这是「没跑」，不是「跑过且没问题」。"; exit 3; }
ts()   { date -u +%Y-%m-%dT%H:%M:%SZ; }
ts_ago() { date -u -d "-$1 seconds" +%Y-%m-%dT%H:%M:%SZ; }

winpath() {
  if have cygpath; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

compose_fault() {
  # 路径必须转成 Windows 形式：脚本开头 export MSYS_NO_PATHCONV=1（为了保住容器内
  # 的 /tmp 之类路径），而 docker 是原生 Windows 程序，`-f /c/...` 它读不到，
  # 表现是「compose 直接非 0 退出、没有任何解释」。
  docker compose -f "$(winpath "$COMPOSE_BASE")" -p drill \
    --env-file "$(winpath "$COMPOSE_ENV")" "$@"
}

cleanup() {
  local rc=$?
  # 兜底：Milvus 一定要拉起来，否则整个开发栈的检索都是坏的
  if [[ "$(docker inspect -f '{{.State.Running}}' "$MILVUS_CONTAINER" 2>/dev/null || echo true)" != "true" ]]; then
    docker start "$MILVUS_CONTAINER" >/dev/null 2>&1 || true
  fi
  # 源码注入必须无条件还原。正常路径里 C 步末尾会 cp 回来，但脚本一旦在
  # 那之前退出（Ctrl-C / 超时 / 断言中断），留下的就是一个坏掉的 chat 路由。
  if [[ -n "$STREAM_BAK" && -f "$STREAM_BAK" ]]; then
    cp "$STREAM_BAK" "$STREAM_TARGET" 2>/dev/null || true
    echo "（已还原 $STREAM_TARGET）"
  fi
  if [[ -n "$KB_ID" && -n "$TOKEN" ]]; then
    curl -s -o "$(winpath /dev/null)" -X DELETE -H "Authorization: Bearer $TOKEN" \
      "$API_BASE/knowledge_bases/$KB_ID" >/dev/null 2>&1 || true
  fi
  if [[ "${KEEP:-0}" != "1" ]]; then
    rm -rf "$TMP_DIR"
  else
    echo "[keep] 保留 $TMP_DIR"
  fi
  return $rc
}
trap cleanup EXIT

wait_live() { # $1=超时秒数
  local deadline=$(( $(date +%s) + ${1:-120} ))
  while (( $(date +%s) < deadline )); do
    if docker exec "$BACKEND" curl -sf -o /dev/null http://127.0.0.1:8000/health/live 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

logs_since() { docker logs "$BACKEND" --since "$1" 2>&1; }

# 日志进 docker logs 有零点几秒延迟：C 步实测 500 已经返回、curl 之后立刻 grep
# 却找不到那行（Python 已 flush，延迟在 json-file driver 落盘这一段）。
# 等一小会儿再判，避免把「日志晚到」误判成「没记日志」——那会让人去查一个
# 不存在的缺陷，反而掩盖真正的问题。超时上限仍然会失败，不会无限等。
wait_logs() { # $1=pattern $2=since $3=超时秒（默认 $LOG_WAIT）
  local pat="$1" since="$2" budget="${3:-$LOG_WAIT}"
  local wide deadline hits
  wide="$(ts_ago 120)"
  deadline=$(( $(date +%s) + budget ))
  while (( $(date +%s) <= deadline )); do
    # 注意：这里**不能**用 `docker logs ... | grep -q`。脚本开了 pipefail，
    # grep -q 命中即退出 → docker 收到 SIGPIPE → 管道整体返回 141，
    # 于是「找到了」被当成失败（2026-11-08 反复踩这个坑：日志明明在，
    # 断言却说没有）。用 grep -c 读完整个流，退出码才可靠。
    hits="$(logs_since "$since" | grep -c "$pat" || true)"
    if [[ "${hits:-0}" -gt 0 ]]; then
      return 0
    fi
    # 兜底窗口：容器 restart 之后，--since t0 偶发会把重启前后的日志切掉
    # （t0 与容器日志时钟之间有毫秒级错位）。放宽到 t0-120s 再判，
    # 判据仍然是「这条 action 出现过」；三个 action 各自唯一（A/B/C 互不重叠），
    # 所以放宽时间窗不会把上一段的证据算到这一段头上。
    hits="$(logs_since "$wide" | grep -c "$pat" || true)"
    if [[ "${hits:-0}" -gt 0 ]]; then
      echo "        （注：命中的日志早于 t0=$since，按 t0-120s 起的窗口判定）"
      return 0
    fi
    sleep 1
  done
  return 1
}

# pause/unpause 之后 /health/live 立刻就是 200（它不检查依赖），只有 /health/ready
# 能反映「Milvus 真的回来了」。恢复后的第一次问答还要给 pymilvus 一点重连时间。
wait_ready() {
  local deadline=$(( $(date +%s) + ${1:-120} ))
  while (( $(date +%s) < deadline )); do
    if docker exec "$BACKEND" curl -sf -o /dev/null http://127.0.0.1:8000/health/ready 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# 语义缓存会让「同一问题」直接命中，根本不走检索/重排——那样 A、B 两类注入都会
# 「看起来正常」。实测（2026-11-08）：不清缓存时，Milvus 停掉后问答照样返回 9 字答案，
# 连 rerank 都没被调用，三处 FAIL 全是这个原因。每次问之前先清该 KB 的缓存。
flush_cache() {
  docker exec -i -e D_KB="${KB_ID:-}" "$BACKEND" python -W ignore - >/dev/null 2>&1 <<'PY' || true
import asyncio, os
from src.services.semantic_cache_service import SemanticCacheService

async def main():
    kb = os.environ.get("D_KB") or ""
    if not kb:
        return
    try:
        await SemanticCacheService().invalidate_kb(kb)
    except Exception:
        pass

asyncio.run(main())
PY
}

# 问一句。输出一行 JSON（见 scripts/ask_once.py），退出码 0/2/1 原样返回。
ask() {
  local q="${1:-$QA_QUESTION}" budget="${2:-120}"
  flush_cache
  set +e
  # ask_once.py 自己会拼 "/api/chat/stream"，所以这里传**不带 /api 的根地址**，
  # 否则会变成 /api/api/chat/stream（404）。
  # 脚本路径要转 Windows 形式：原生 python 会把 /c/... 解析成 C:\c\...
  timeout "$budget" "$PYTHON_BIN" "$(winpath "$ROOT_DIR/scripts/ask_once.py")" \
    "${API_BASE%/api}" "$TOKEN" "${KB_ID:-}" "$q" 2>&1
  local rc=$?
  set -e
  return $rc
}

json_field() { # $1=json $2=key
  printf '%s' "$1" | "$PYTHON_BIN" -c "import json,sys
try: print(json.load(sys.stdin).get('$2',''))
except Exception: print('')" 2>/dev/null
}

step "0/5 前置检查"
[[ -n "$PYTHON_BIN" ]] || skip "宿主机没有 python（ask_once.py 需要）"
docker info >/dev/null 2>&1 || skip "Docker 不可用"
for c in "$MILVUS_CONTAINER" postgres-dev minio-dev redis-dev; do
  docker inspect "$c" >/dev/null 2>&1 || skip "缺少开发栈容器 $c（先跑 ./scripts/start-dev.sh）"
done

mkdir -p "$TMP_DIR"

# 专用后端：与开发栈同镜像、独立容器，故障注入不污染 backend-dev
if ! docker inspect "$BACKEND" >/dev/null 2>&1; then
  echo "  启动 $BACKEND（首次会构建镜像）…"
fi
# 容器可能停在「上一次启动时 Milvus 恰好没起来」的失败态：进程已退出、
# 容器还显示 Up。这种状态 up -d 不会重建，必须先探活再决定。
if docker exec "$BACKEND" curl -sf -o /dev/null http://127.0.0.1:8000/health/live 2>/dev/null; then
  compose_fault up -d >/dev/null 2>&1 || true
else
  echo "  $BACKEND 无响应，强制重建…"
  if ! compose_fault up -d --force-recreate --no-build > "$TMP_DIR/compose-up.log" 2>&1; then
    compose_fault up -d --force-recreate > "$TMP_DIR/compose-up.log" 2>&1 || true
  fi
fi
wait_live 240 || {
  tail -15 "$TMP_DIR/compose-up.log" 2>/dev/null >&2 || true
  skip "fault-backend 240s 内未就绪（日志见 $TMP_DIR/compose-up.log / docker logs $BACKEND）"
}
ok "fault-backend 就绪"

# 登录（dev 库里的固定演练账号；不存在则注册）
auth="{\"username\":\"$DRILL_USER\",\"password\":\"$DRILL_PASS\"}"
resp="$(curl -s -X POST "$API_BASE/auth/login" -H 'Content-Type: application/json' -d "$auth" || true)"
TOKEN="$(json_field "$resp" access_token)"
if [[ -z "$TOKEN" ]]; then
  resp="$(curl -s -X POST "$API_BASE/auth/register" -H 'Content-Type: application/json' -d "$auth" || true)"
  TOKEN="$(json_field "$resp" access_token)"
fi
[[ -n "$TOKEN" ]] || skip "既登录不上也注册不了演练账号"
ok "已登录（token ${#TOKEN} 字符）"

# 每次新建一个专用知识库，**不复用**已有库。
# 复用会踩到一个很隐蔽的状态：PG 里 documents.chunks_count > 0，但 Milvus 里
# 该库一条向量都没有（2026-11-08 实测开发栈只剩 2 条向量，是多轮演练/重启留下的漂移）。
# 这时检索返回 0 条候选 → 根本没有 pair 可打分 → 注入 B 静默失效，
# 表面上「问答成功、没有报错」，实际什么都没测到。
resp="$(curl -s -X POST "$API_BASE/knowledge_bases/" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d "{\"name\":\"fault-kb-$(date +%H%M%S)\"}")"
KB_ID="$(json_field "$resp" id)"
[[ -n "$KB_ID" ]] || skip "建知识库失败（$resp）"
ok "kb_id=$KB_ID（本次新建，结束时删除）"

# 内容要包含问题里的关键词，否则检索会 kb_miss，rerank 注入就永远触发不到
{
  echo "故障演练文档"
  for i in $(seq 1 12); do
    echo "第 $i 段：这份故障演练文档用于验证删除链与错误分级。故障演练会停掉 Milvus、"
    echo "把重排序模型指向不存在的名称，并临时注入未捕获异常，观察日志能否区分这三类问题。"
  done
} > "$TMP_DIR/fault.txt"
resp="$(curl -s -X POST "$API_BASE/documents/upload?kb_id=$KB_ID" -H "Authorization: Bearer $TOKEN" \
  -F "file=@$(winpath "$TMP_DIR/fault.txt");type=text/plain")"
DOC_ID="$(json_field "$resp" id)"
[[ -n "$DOC_ID" ]] || skip "上传演练文档失败（$resp）"
state=""
for _ in $(seq 1 60); do
  state="$(curl -s "$API_BASE/documents/?kb_id=$KB_ID" -H "Authorization: Bearer $TOKEN" \
    | "$PYTHON_BIN" -c '
import json,sys
try:
    rows = json.load(sys.stdin)
    print((rows[0].get("processing_status") or "") if rows else "")
except Exception: print("")')"
  [[ "$state" == "completed" ]] && break
  [[ "$state" == "failed" ]] && skip "文档处理失败（先确认开发栈的 Ollama / NLTK 正常）"
  sleep 2
done
[[ "$state" == "completed" ]] || skip "文档处理超时（状态 ${state:-未知}）"
ok "演练文档已处理完成（doc_id=$DOC_ID）"

# 必须显式 flush：Milvus 的插入先进内存中的 growing segment，只有 flush 之后才落到
# MinIO。插入后一两分钟内 `docker stop` Milvus，未 flush 的数据会一起消失——实测
# 2026-11-08：基线能检索到 3 条来源，A 步停一次 Milvus 之后 B 步候选变 0，
# 注入 B 就静默失效了（不是代码问题，是「测试对象自己把数据弄丢了」）。
docker exec "$BACKEND" python -W ignore -c "
from pymilvus import MilvusClient
MilvusClient(uri='http://milvus-standalone:19530').flush('documents')
" >/dev/null 2>&1 || true
ok "已 flush 向量（保证 A 步停 Milvus 不会把演练数据弄丢）"
# 基线要能证明「有候选」：只看退出码不足以说明检索通了（见上方漂移说明）
step "1/5 基线：先证明注入前是好的"
base_rc=0
base_out="$(ask "" 90)" || base_rc=$?
[[ $base_rc -eq 0 ]] || { echo "  $base_out" >&2; skip "基线问答就不通，故障演练没有意义"; }
base_src="$(json_field "$base_out" sources)"
if [[ "${base_src:-0}" -le 0 ]]; then
  echo "  $base_out" >&2
  skip "基线问答 0 条来源：向量没进 Milvus（PG 与 Milvus 不一致），注入 B 会静默失效。先跑 python scripts/consistency_audit.py"
fi
ok "基线问答完成（$(json_field "$base_out" answer_len) 字，${base_src} 条来源，$(json_field "$base_out" answer_type)）"

step "2/5 注入 A：Milvus 不可达"
# 用 stop/start 而不是 pause/unpause：实测（2026-11-08）pause 期间 Milvus 的
# etcd 租约到期，进程会自行退出（日志：`confirm the lease is expired, the session
# is expired without activing closing`），unpause 回来是个死容器。
# stop/start 既更接近「依赖真的挂了」，恢复也可控。
t0="$(ts)"
docker stop "$MILVUS_CONTAINER" >/dev/null
ok "已停止 $MILVUS_CONTAINER"
ask_rc=0
ask_out="$(ask "" 120)" || ask_rc=$?
docker start "$MILVUS_CONTAINER" >/dev/null
ok "已启动 $MILVUS_CONTAINER（等它就绪）"

if [[ $ask_rc -eq 0 ]]; then
  bad "Milvus 停机时问答仍然「成功」了——说明检索失败被吞掉，用户会看到一个假答案"
else
  ok "问答没有返回成功（退出码 $ask_rc）"
fi
# 关键判据：失败必须是「错误」，不能伪装成「知识库里没有相关内容」
if printf '%s' "$ask_out" | grep -qE "没有找到|未找到相关|没有相关内容"; then
  bad "用户侧看到的是「没有相关内容」——把基础设施故障说成内容缺失，这是最容易误导的一类"
else
  ok "没有伪装成「没有相关内容」"
fi

if wait_logs "action=kb.retrieval.dense_fallback" "$t0"; then
  a_logs="$(logs_since "$t0")"
  if printf '%s' "$a_logs" | grep "action=kb.retrieval.dense_fallback" | grep -q "severity=reportable"; then
    ok "日志分级正确：action=kb.retrieval.dense_fallback severity=reportable"
  else
    bad "dense 兜底失败只记了 recoverable（真实事故会被当成抖动）"
  fi
else
  bad "没有找到 action=kb.retrieval.dense_fallback 的日志（检索失败没有留痕？）"
fi

wait_ready 180 || skip "Milvus 重启后后端未恢复（ready 不通过）"
recover_rc=0
recover_out="$(ask "" 90)" || recover_rc=$?
if [[ $recover_rc -ne 0 ]]; then
  # Milvus 刚恢复时 pymilvus 还要重连一次，给一次重试再判失败
  sleep 5
  recover_rc=0
  recover_out="$(ask "" 90)" || recover_rc=$?
fi
[[ $recover_rc -eq 0 ]] && ok "恢复后问答正常" || bad "恢复后问答仍失败（退出码 $recover_rc）：$recover_out"
# 恢复不只要能答，还要**答得有依据**：Milvus 重启后若向量丢了，问答会以
# 「0 条来源的直答」形式"正常"返回，下一段 B 就再也触发不到重排序。
rec_src="$(json_field "$recover_out" sources)"
if [[ $recover_rc -eq 0 && "${rec_src:-0}" -le 0 ]]; then
  bad "Milvus 重启后检索到 0 条来源（向量没持久化？insert 后未 flush 就 stop 会丢）"
elif [[ $recover_rc -eq 0 ]]; then
  ok "恢复后仍有 ${rec_src} 条来源（数据没被重启弄丢）"
fi

step "3/5 注入 B：reranker 指向不存在的模型"
t0="$(ts)"
# 必须同时把 provider 改成 ollama：如果只改模型名而 provider 仍是
# sentence_transformers，失败发生在「加载」阶段（hybrid_search.load_cross_encoder），
# 进程随后把 _model_failed 置真、永久走 RRF 截断——打分路径一次都不会进入，
# 也就验证不到「连续失败升级」。实测（2026-11-08 01:33）就是这么假通过的：
# 三次问答全 0 退出、日志里一条 hybrid_search.rerank 都没有。
# ollama 的坏模型名是「每次打分都 404」（实测 0.04s 返回），正好打中打分路径。
cat > "$TMP_DIR/override.yml" <<'YAML'
services:
  backend:
    environment:
      KB_RERANK_PROVIDER: "ollama"
      KB_RERANK_MODEL: "no-such-reranker:0b"
YAML
if ! docker compose -f "$(winpath "$COMPOSE_BASE")" -f "$(winpath "$TMP_DIR/override.yml")" \
  -p drill --env-file "$(winpath "$COMPOSE_ENV")" up -d --force-recreate --no-build backend \
  > "$TMP_DIR/override-up.log" 2>&1; then
  tail -10 "$TMP_DIR/override-up.log" >&2
  skip "坏 reranker 配置未能应用（compose 输出见上）"
fi
wait_ready 180 || skip "坏 reranker 配置下后端未就绪（说明加载阶段就炸了，需单独看）"
applied="$(docker exec "$BACKEND" printenv KB_RERANK_MODEL 2>/dev/null || true)"
applied_provider="$(docker exec "$BACKEND" printenv KB_RERANK_PROVIDER 2>/dev/null || true)"
if [[ "$applied" != "no-such-reranker:0b" || "$applied_provider" != "ollama" ]]; then
  bad "配置没生效：KB_RERANK_MODEL=${applied:-空} provider=${applied_provider:-空}（期望 no-such-reranker:0b / ollama）"
else
  ok "已用坏 reranker 重启后端（provider=$applied_provider model=$applied）"
fi

# 连续三次：分级是「连续失败」才升级，单次抖动必须仍是 recoverable
for i in 1 2 3; do
  rc=0
  out="$(ask "" 90)" || rc=$?
  # 判据是**来源条数**，不是答案文案：提示词本身就让模型在上下文不足时
  # 说「无法找到相关信息」，拿这句话当「伪装成没有内容」等于把模型的正常
  # 拒答算成故障（2026-11-08 实测就是这么误报的）。有来源但排序变差，
  # 才是重排失败的正常表现。
  src="$(json_field "$out" sources)"
  echo "  第 $i 次：退出码 $rc，答案 $(json_field "$out" answer_len) 字，来源 ${src:-0} 条"
  if [[ $rc -eq 0 && "${src:-0}" -le 0 ]]; then
    bad "第 $i 次：重排失败后连来源都没了（候选被丢光，等价于「没有内容」）"
  fi
done

if wait_logs "action=hybrid_search.rerank" "$t0"; then
  b_logs="$(logs_since "$t0")"
  b_lines="$(printf '%s' "$b_logs" | grep "action=hybrid_search.rerank" || true)"
  # 把真实异常打到演练输出里：只看「有日志」看不出失败原因，
  # 而这一行的 error= 字段正是下次排障的入口。
  printf '        %s\n' "$(printf '%s' "$b_lines" | head -1 | cut -c1-260)"
  if printf '%s' "$b_logs" | grep -q "action=rerank.ollama.score"; then
    ok "底层打分确实失败（action=rerank.ollama.score，不是别的原因导致没跑）"
  elif printf '%s' "$b_lines" | grep -q "重排打分失败"; then
    # ollama_reranker 对单条失败做了节流（前 3 次逐条记，之后每 50 次补一条），
    # 而启动预热就已经把前 3 次额度用掉了 —— 演练期间的逐条日志会被抑制，
    # 但 batch 级结论（「整批 N/N 条打分失败」）仍然在。用后者判定，
    # 比为了凑一条日志去改生产环境的日志节流策略合理。
    ok "底层打分确实失败（整批打分为空；逐条日志被节流，预热已用掉前 3 次额度）"
  else
    bad "有 hybrid_search.rerank 但没有打分失败的证据：失败原因不明（只剩 RRF 回退分）"
  fi
else
  bad "没有 action=hybrid_search.rerank 日志（打分失败没有留痕）"
  # 只有加载阶段失败时才会走到这里：给出确证，避免「什么都没测到」被当成脚本问题
  if logs_since "$t0" | grep -q "action=hybrid_search.load_cross_encoder"; then
    echo "        注：本次失败发生在模型加载阶段（load_cross_encoder），打分路径未被触发"
  fi
fi
if [[ -n "${b_lines:-}" ]]; then
  if printf '%s' "$b_lines" | grep -q "severity=recoverable"; then
    ok "单次失败仍按 recoverable 记（没有把抖动当事故）"
  fi
  if printf '%s' "$b_lines" | grep -q "consecutive_failures=3"; then
    if printf '%s' "$b_lines" | grep "consecutive_failures=3" | grep -q "severity=reportable"; then
      ok "连续第 3 次失败升级为 reportable"
    else
      bad "连续失败没有升级分级（长期坏着也不会有人知道）"
    fi
  else
    bad "没有 consecutive_failures 计数（无法区分抖动与持续失效）"
  fi
fi

# 恢复：回到正常配置
compose_fault up -d --force-recreate --no-build backend >/dev/null 2>&1
wait_ready 180 || skip "恢复配置后后端未就绪"
recover_rc=0
recover_out="$(ask "" 90)" || recover_rc=$?
[[ $recover_rc -eq 0 ]] && ok "恢复 reranker 后问答正常" || bad "恢复 reranker 后问答失败"

step "4/5 注入 C：代码 bug（未捕获异常）"
t0="$(ts)"
STREAM_BAK="$TMP_DIR/stream.py.bak"
cp "$STREAM_TARGET" "$STREAM_BAK"
"$PYTHON_BIN" - "$(winpath "$STREAM_TARGET")" <<'PY'
import sys
path = sys.argv[1]
src = open(path, encoding="utf-8").read()
anchor = "    question = payload.question\n"
if anchor not in src:
    raise SystemExit("注入锚点不存在，脚本需要更新")
src = src.replace(
    anchor,
    "    raise RuntimeError('fault drill: injected unhandled error')\n" + anchor,
    1,
)
open(path, "w", encoding="utf-8").write(src)
PY
ok "已注入未捕获异常，重启后端让改动生效"

# 不依赖 uvicorn --reload：在 Windows 挂载卷上 watchfiles 不一定收到事件
# （实测 2026-11-08：改完文件请求照样 200，注入等于没做）。直接重启，确定性更高。
docker restart "$BACKEND" >/dev/null
wait_ready 240 || skip "注入后后端未就绪"

code="$(curl -s -o "$(winpath "$TMP_DIR/bug.json")" -w '%{http_code}' -X POST "$API_BASE/chat/stream" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"question\":\"$QA_QUESTION\"}")"
body="$(cat "$TMP_DIR/bug.json" 2>/dev/null || true)"
if [[ "$code" == "500" ]]; then
  ok "返回 500"
else
  bad "期望 500，实际 $code"
fi
if printf '%s' "$body" | grep -q '"request_id"'; then
  ok "响应里带 request_id（用户报障时能对上日志）"
else
  bad "500 响应没有 request_id：$(printf '%s' "$body" | head -c 160)"
fi

if wait_logs "action=request.unhandled" "$t0"; then
  c_logs="$(logs_since "$t0")"
  if printf '%s' "$c_logs" | grep "action=request.unhandled" | grep -q "severity=programming"; then
    ok "日志分级正确：severity=programming"
  else
    bad "未捕获异常的 severity 不是 programming"
  fi
  # 堆栈和正文是同一条日志记录的两个部分，但到 docker logs 的落盘是分行追加的：
  # 只等正文出现就立刻 grep 堆栈，会偶尔只看到前几行（2026-11-08 实测）。
  if wait_logs "Traceback (most recent call last)" "$t0"; then
    ok "带堆栈"
  else
    bad "没有堆栈，定位不到是哪一行"
  fi
else
  # 这一条 2026-11-08 曾误报：500 与 request_id 都对，日志也确实写了，
  # 但 grep 在 curl 返回的同一秒执行，json-file driver 还没落盘。
  bad "没有 action=request.unhandled 日志（等了 ${LOG_WAIT:-20}s 仍没有）"
fi

# 恢复源码并确认服务回到正常
cp "$STREAM_BAK" "$STREAM_TARGET"
STREAM_BAK=""
docker restart "$BACKEND" >/dev/null
wait_ready 240 || skip "恢复源码后后端未就绪"
recover_rc=0
recover_out="$(ask "" 90)" || recover_rc=$?
[[ $recover_rc -eq 0 ]] && ok "恢复源码后问答正常" || bad "恢复源码后问答仍失败"

step "5/5 结果"
if [[ "$FAILED" != "0" ]]; then
  echo "有不符合预期的项。查看后端日志："
  echo "  docker logs $BACKEND --since 15m | grep -E 'severity=|action='"
  exit 1
fi
echo "三类故障均得到预期分级："
echo "  A Milvus 不可达  → kb.retrieval.dense_fallback severity=reportable，用户侧无「没有相关内容」"
echo "  B 打分失败      → hybrid_search.rerank 连续 3 次升为 reportable，分数回退 RRF 而非 0"
echo "  C 代码 bug      → 500 + request_id + request.unhandled severity=programming + 堆栈"
echo "每类注入后均已恢复并复验正常问答。"
