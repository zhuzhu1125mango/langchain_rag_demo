#!/usr/bin/env bash
# 回滚演练（runbook §7.2）：把「上一版镜像」装到「当前版本的数据」上跑一遍。
#
# 为什么必须有这一条：升级前打备份、跑迁移、切镜像，这几步都演练过；但**回滚**从来没跑过。
# 而真实事故里更常见的是「升级完发现不对，想退回上一版」——这时数据库已经被迁移过了，
# 回滚不是「把镜像换回去」这么简单。本次演练要回答四个问题：
#   1. 旧镜像能不能在**新 schema**上起来？（迁移是加列，理论上向后兼容，但没验过）
#   2. 起来之后能不能登录、能不能答对问题？（能启动 ≠ 能用）
#   3. 旧**前端**接上去还能不能正常用？（§5.2 要求前后端同批回退；SSE 会不会被 nginx 缓冲）
#   4. 回滚会丢掉什么？（旧镜像没有 P0-4 的优雅停机，SSE 生成中会被硬杀）
#
# 安全边界（刻意的）：
#   - 只创建/删除名字以 drill-rb- 开头的容器、卷与网络；不碰生产容器与生产卷
#   - 从生产**读**一份新备份、从生产 Milvus/MinIO **读**（向量与原件不写）
#   - 写操作只落在 drill-rb-pg 与 drill-rb-redis 这两个一次性实例上
#   - backend 用 8003、frontend 用 8081、PG 用 5548（避开生产 8080/5434、开发 5433、恢复演练 5547）
#
# 用法：
#   ./scripts/rollback_drill.sh                                  # 现打一份备份，回滚到默认镜像
#   ROLLBACK_IMAGE=rag-backend:rollback-20261101 ./scripts/rollback_drill.sh
#   FRONTEND_IMAGE=rag-frontend:rollback-20261101b ./scripts/rollback_drill.sh
#   PG_FILE=backups/xxx.sql.gz ./scripts/rollback_drill.sh       # 用指定备份（省一次 dump）
#   KEEP=1 ./scripts/rollback_drill.sh                           # 保留现场
#   DEBUG=1 ./scripts/rollback_drill.sh                          # 出错时打印行号
set -euo pipefail
# DEBUG=1 时打印出错行：这个脚本在 Git Bash 下的坑（原生 curl + MSYS 路径）几乎都不报错就退出
if [[ "${DEBUG:-0}" == "1" ]]; then
  trap 'echo "ERR line $LINENO: $BASH_COMMAND" >&2' ERR
fi

export MSYS_NO_PATHCONV=1   # 容器内 /run/secrets/... 与 /tmp/... 不能被改写成 Windows 路径

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ROLLBACK_IMAGE="${ROLLBACK_IMAGE:-rag-backend:rollback-20261101b}"
NET="${NET:-rag-prod_infra-net}"
DRILL_PG="drill-rb-pg"
DRILL_VOL="drill_rb_pg_data"
DRILL_REDIS="drill-rb-redis"
DRILL_BACKEND="drill-rb-backend"
DRILL_FRONTEND="drill-rb-frontend"
DRILL_NET="drill-rb-net"        # 只用于把别名 backend 指到演练后端，用完即删
FRONTEND_IMAGE="${FRONTEND_IMAGE:-rag-frontend:rollback-20261101b}"
DRILL_PORT="${DRILL_PORT:-8003}"
FRONTEND_PORT="${FRONTEND_PORT:-8081}"
PG_PORT="${PG_PORT:-5548}"
DRILL_DB="app_prod"     # 与生产同名同用户：回滚时环境里只应差「连哪台」，不该差名字
DRILL_USER="prod_user"
KEEP="${KEEP:-0}"
DRILL_PASSWORD="RollbackDrill2026"   # 只在演练库里存在的临时账号口令
# 问答用哪个知识库：默认取「演练账号自己名下的第一个」——权限隔离下，拿别人的 KB 问不出东西，
# 演练就会失败在一个与被测对象无关的原因上。可用 ASK_KB / ASK_Q 覆盖（换语料时）。
ASK_KB="${ASK_KB:-}"
# 两个默认问题都对**本仓库的验收语料**（docs/ 那 4 份文档）有效；换语料时用 ASK_Q / ASK_Q_STOP 覆盖。
# 问答题要求「能命中知识库」，只是为了证明旧镜像能读出当前数据里的向量；
# 停机题要求「答得长」，好让停机真的落在生成过程中。
ASK_Q="${ASK_Q:-备份怎么恢复？恢复后要确认什么？}"
ASK_Q_STOP="${ASK_Q_STOP:-这个系统的整体架构是怎样的？请求经过哪些组件？}"
# 前端那条单独用另一道题：同一道题刚问过，命中语义缓存就变成「秒回」，
# 测到的不是「代理不缓冲」而是「根本没在生成」。换题比清 Redis 更干净。
ASK_Q_PROXY="${ASK_Q_PROXY:-优雅停机是怎么实现的？}"

WORK="$REPO_ROOT/backups/rollback-drill"
mkdir -p "$WORK"
SECRETS="$REPO_ROOT/secrets"

have() { command -v "$1" >/dev/null 2>&1; }
# 原生 curl 不认识 /dev/null：MSYS_NO_PATHCONV=1 之后它收到的是字面量 `/dev/null`，
# 会当成 C://dev//null 去写 -> 退出码 23（写失败），而 -w 照样打印 200，极难发现。
NULL_DEV="$(cygpath -w /dev/null 2>/dev/null || echo /dev/null)"
winpath() { if have cygpath; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
pg() { docker exec "$DRILL_PG" psql -U "$DRILL_USER" -d "$DRILL_DB" -tAc "$1" | tr -d '[:space:]'; }

cleanup() {
  if [[ "$KEEP" == "1" ]]; then
    echo "（KEEP=1，保留 $DRILL_BACKEND / $DRILL_FRONTEND / $DRILL_REDIS / $DRILL_PG 供排查）"
    return
  fi
  docker rm -f "$DRILL_FRONTEND" "$DRILL_BACKEND" "$DRILL_REDIS" "$DRILL_PG" >/dev/null 2>&1 || true
  docker network disconnect "$DRILL_NET" "$DRILL_BACKEND" >/dev/null 2>&1 || true
  docker network rm "$DRILL_NET" >/dev/null 2>&1 || true
  docker volume rm "$DRILL_VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1 || {
  echo "找不到回滚镜像 $ROLLBACK_IMAGE" >&2; exit 1; }

echo "== 回滚演练 =="
echo "回滚镜像: $ROLLBACK_IMAGE（$(docker image inspect "$ROLLBACK_IMAGE" --format '{{.Created}}' | cut -c1-10) 构建）"
echo ""

# ---------- 1. 取一份「当前」备份 ----------
t0=$(date +%s)
if [[ -n "${PG_FILE:-}" ]]; then
  [[ -f "$PG_FILE" ]] || { echo "PG_FILE 不存在: $PG_FILE" >&2; exit 1; }
  echo "[1/7] 使用指定备份: $PG_FILE"
else
  echo "[1/7] 从生产现打一份备份（模拟「升级完、数据已到新 schema」的现场）"
  BACKUP_DIR="$WORK/backup" MINIO_BUCKET=documents ./scripts/backup.sh
  PG_FILE="$(ls -t "$WORK"/backup/*.sql.gz 2>/dev/null | head -1)"
  [[ -n "$PG_FILE" ]] || { echo "备份未产出" >&2; exit 1; }
fi

# ---------- 2. 隔离 PG + 恢复（宿主机执行 restore.sh，走容器降级分支） ----------
echo ""
echo "[2/7] 起隔离 PG 并恢复（restore.sh 在宿主机执行：本机无 psql，走容器降级分支）"
docker rm -f "$DRILL_PG" >/dev/null 2>&1 || true
docker volume rm "$DRILL_VOL" >/dev/null 2>&1 || true
docker run -d --name "$DRILL_PG" --network "$NET" \
  -e "POSTGRES_PASSWORD=$(cat "$SECRETS/postgres_password")" \
  -e "POSTGRES_DB=$DRILL_DB" -e "POSTGRES_USER=$DRILL_USER" \
  -p "127.0.0.1:$PG_PORT:5432" -v "$DRILL_VOL:/var/lib/postgresql/data" \
  postgres:16.14 >/dev/null
for _ in $(seq 1 60); do
  docker exec "$DRILL_PG" pg_isready -U "$DRILL_USER" -d "$DRILL_DB" >/dev/null 2>&1 && break
  sleep 1
done

PG_CONTAINER="$DRILL_PG" PG_FILE="$PG_FILE" \
  POSTGRES_DB="$DRILL_DB" POSTGRES_USER="$DRILL_USER" TARGET_EMPTY=1 \
  bash scripts/restore.sh | sed 's/^/      /'

# ---------- 3. schema 版本核对：新库 vs 旧镜像 ----------
echo ""
echo "[3/7] schema 版本核对"
db_rev=$(pg "select version_num from alembic_version")
img_rev=$(docker run --rm --entrypoint alembic "$ROLLBACK_IMAGE" heads 2>/dev/null | awk '{print $1}' || true)
echo "      还原库 alembic_version = $db_rev"
echo "      回滚镜像内置 head     = $img_rev"
if [[ "$db_rev" == "$img_rev" ]]; then
  echo "      → 版本一致，回滚无 schema 缺口"
else
  echo "      → 版本不一致：库比镜像新。这是**回滚的常态**（迁移只加列），下面验证旧镜像能否工作"
  echo "      → 同时验证一个反直觉的操作事实：旧镜像跑不了 alembic（它不认识这个 revision）"
  set +e
  ALEMBIC_OUT=$(docker run --rm --network "$NET" \
    -e POSTGRES_HOST="$DRILL_PG" -e POSTGRES_PORT=5432 \
    -e POSTGRES_DB="$DRILL_DB" -e POSTGRES_USER="$DRILL_USER" \
    -v "$(winpath "$SECRETS/postgres_password"):/run/secrets/POSTGRES_PASSWORD" \
    --entrypoint alembic "$ROLLBACK_IMAGE" current 2>&1)
  set -e
  # 只留结论行：alembic 失败时会打一屏 SQLAlchemy traceback，那是「连不上」的副作用，不是重点
  echo "$ALEMBIC_OUT" | grep -iE "can't locate|revision|error|FAILED" | tail -3 | sed 's/^/        | /' || true
fi

# ---------- 4. 起 Redis + 旧镜像 backend ----------
echo ""
echo "[4/7] 起 drill Redis + 旧镜像 backend（连生产 Milvus/MinIO 只读）"
docker rm -f "$DRILL_REDIS" >/dev/null 2>&1 || true
docker run -d --name "$DRILL_REDIS" --network "$NET" \
  -v "$(winpath "$SECRETS/redis_password"):/run/secrets/REDIS_PASSWORD" \
  redis:7.2-alpine sh -c 'redis-server --requirepass "$(cat /run/secrets/REDIS_PASSWORD)"' >/dev/null

docker inspect backend-prod --format '{{range .Config.Env}}{{println .}}{{end}}' > "$WORK/backend.env"
t_backend=$(date +%s)
docker rm -f "$DRILL_BACKEND" >/dev/null 2>&1 || true
docker run -d --name "$DRILL_BACKEND" --network "$NET" \
  --env-file "$(winpath "$WORK/backend.env")" \
  -e POSTGRES_HOST="$DRILL_PG" -e REDIS_HOST="$DRILL_REDIS" \
  -e CONSISTENCY_AUDIT_INTERVAL_HOURS=0 \
  -p "127.0.0.1:$DRILL_PORT:8000" \
  -v "$(winpath "$SECRETS/admin_key"):/run/secrets/ADMIN_KEY" \
  -v "$(winpath "$SECRETS/metrics_token"):/run/secrets/METRICS_TOKEN" \
  -v "$(winpath "$SECRETS/minio_root_user"):/run/secrets/MINIO_ACCESS_KEY" \
  -v "$(winpath "$SECRETS/minio_root_password"):/run/secrets/MINIO_SECRET_KEY" \
  -v "$(winpath "$SECRETS/postgres_password"):/run/secrets/POSTGRES_PASSWORD" \
  -v "$(winpath "$SECRETS/redis_password"):/run/secrets/REDIS_PASSWORD" \
  -v "$(winpath "$SECRETS/search_api_key"):/run/secrets/SEARCH_API_KEY" \
  -v "$(winpath "$SECRETS/secret_key"):/run/secrets/SECRET_KEY" \
  -v "$(winpath "$REPO_ROOT/backend/data"):/app/data" \
  "$ROLLBACK_IMAGE" >/dev/null

ready=0
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$DRILL_PORT/health/ready" >/dev/null 2>&1; then ready=1; break; fi
  if ! docker ps --format '{{.Names}}' | grep -qx "$DRILL_BACKEND"; then break; fi
  sleep 2
done
t_ready=$(( $(date +%s) - t_backend ))
if [[ "$ready" != "1" ]]; then
  echo "      ✗ 旧镜像未就绪（${t_ready}s）。容器日志尾部："
  docker logs --tail 30 "$DRILL_BACKEND" 2>&1 | sed 's/^/        | /'
  exit 1
fi
echo "      ✓ /health/ready 200，耗时 ${t_ready}s"
curl -s "http://127.0.0.1:$DRILL_PORT/health/ready" | head -c 300; echo

# ---------- 5. 功能验收：登录 + 问答 ----------
echo ""
echo "[5/7] 功能验收（旧镜像 + 新数据）"
# 演练账号与知识库**不写死**：换一台机器、换一批语料也能跑。优先取管理员（有 KB 的概率最高），
# 再取它自己名下的第一个 KB —— 权限隔离下用别人的 KB 问不出东西，会失败在与被测对象无关的原因上。
DRILL_USERNAME="${DRILL_USERNAME:-$(pg "select username from users where is_admin order by created_at limit 1")}"
if [[ -z "$DRILL_USERNAME" ]]; then
  echo "      ！还原库里没有管理员账号，跳过登录与问答（可先跑 scripts/create_admin.py 再演练）"
  SKIP_QA=1
else
  SKIP_QA=0
  user_id=$(pg "select id from users where username = '$DRILL_USERNAME'")
  ASK_KB="${ASK_KB:-$(pg "select id from knowledge_bases where owner_id = '$user_id' order by created_at limit 1")}"
  ask_doc=$(pg "select filename from documents where kb_id = '$ASK_KB' order by created_at limit 1" || true)
  echo "      演练账号: $DRILL_USERNAME（$user_id），知识库: ${ASK_KB:-（该账号名下没有 KB）}，首篇文档: ${ask_doc:-（空）}"
  hash=$(docker exec backend-prod python -c "from src.auth import hash_password; print(hash_password('$DRILL_PASSWORD'))")
  printf "update users set password_hash = '%s' where username = '%s';\n" "$hash" "$DRILL_USERNAME" > "$WORK/reset_pw.sql"
  docker cp "$(winpath "$WORK/reset_pw.sql")" "$DRILL_PG:/tmp/reset_pw.sql" >/dev/null
  docker exec "$DRILL_PG" psql -U "$DRILL_USER" -d "$DRILL_DB" -q -f /tmp/reset_pw.sql
fi

if [[ "$SKIP_QA" == "1" || -z "$ASK_KB" ]]; then
  echo "      ！没有可问的知识库，问答与停机测试跳过（ready 与登录之外的部分未覆盖）"
  SKIP_QA=1
else
TOKEN=$(curl -s -X POST "http://127.0.0.1:$DRILL_PORT/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$DRILL_USERNAME\",\"password\":\"$DRILL_PASSWORD\"}" \
  | python -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))')
if [[ -z "$TOKEN" ]]; then echo "      ✗ 登录失败（旧镜像读不到新 schema 的用户表？）" >&2; exit 1; fi
echo "      ✓ 登录成功（token ${#TOKEN} 字符）"

echo "      问答中：$ASK_Q"
set +e
ask_out=$(python scripts/ask_once.py "http://127.0.0.1:$DRILL_PORT" "$TOKEN" "$ASK_KB" "$ASK_Q" \
  --raw "$(winpath "$WORK/answer.txt")")
ask_rc=$?
set -e
echo "$ask_out" | sed 's/^/      /'
if [[ $ask_rc -ne 0 ]]; then
  echo "      ✗ 流不完整或请求失败（退出码 $ask_rc）" >&2
  exit 1
fi
echo "      ✓ 流完整（收到 end 帧），答案落 $WORK/answer.txt"
# 「链路通」不等于「读得到当前数据的向量」：只有命中知识库才能证明后者。
ask_type=$(printf '%s' "$ask_out" | python -c 'import json,sys;print(json.load(sys.stdin).get("answer_type"))' 2>/dev/null || echo "?")
if [[ "$ask_type" != "knowledge_base" ]]; then
  echo "      ！回答类型是 $ask_type（不是 knowledge_base）：只证明了问答链路通，"
  echo "        没证明能读出当前数据里的向量；换一道能命中这个库的问题再跑：ASK_Q='...'"
fi
fi   # SKIP_QA

# ---------- 6. 前端回滚：旧前端镜像 + 当前后端 ----------
echo ""
echo "[6/7] 前端回滚（旧前端镜像 + 当前后端）：入口页、静态资源、API 代理、SSE 是否被缓冲"
if ! docker image inspect "$FRONTEND_IMAGE" >/dev/null 2>&1; then
  echo "      ！找不到 $FRONTEND_IMAGE，跳过前端回滚"
elif [[ "$SKIP_QA" == "1" ]]; then
  echo "      ！没有可用 token，跳过（SSE 验收需要登录态）"
else
  # 旧镜像的 nginx 写死 proxy_pass http://backend:8000。要让这个别名指向**演练**后端
  # 而不是生产后端，只能在一条独立网络里给它起别名——直接连 infra-net 会打到生产。
  docker network create "$DRILL_NET" >/dev/null 2>&1 || true
  docker network connect --alias backend "$DRILL_NET" "$DRILL_BACKEND" 2>/dev/null || true
  docker rm -f "$DRILL_FRONTEND" >/dev/null 2>&1 || true
  docker run -d --name "$DRILL_FRONTEND" --network "$DRILL_NET" \
    -p "127.0.0.1:$FRONTEND_PORT:8080" "$FRONTEND_IMAGE" >/dev/null
  fe_ready=0
  for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null 2>&1 && { fe_ready=1; break; }
    sleep 1
  done
  if [[ "$fe_ready" != "1" ]]; then
    echo "      ✗ 旧前端没起来，日志尾部："
    docker logs --tail 20 "$DRILL_FRONTEND" 2>&1 | sed 's/^/        | /'
  else
    # -o 的路径必须转成 Windows 形式：curl 是原生二进制，`/c/...` 会被它当成
    # 当前盘的相对路径（C:\c\...），写不进去时 curl 退出码 23，而 -w 照样打印 200，
    # 于是一路「看起来成功」直到下一步 grep 文件不存在才炸。
    code=$(curl -s -o "$(winpath "$WORK/fe_index.html")" -w '%{http_code}' "http://127.0.0.1:$FRONTEND_PORT/")
    has_app=$(grep -c 'id="app"' "$WORK/fe_index.html" || true)
    asset=$(grep -o '/assets/[^"]*\.js' "$WORK/fe_index.html" | head -1 || true)
    asset_code="n/a"; asset_type="n/a"
    if [[ -n "$asset" ]]; then
      asset_code=$(curl -s -o "$NULL_DEV" -w '%{http_code}' "http://127.0.0.1:$FRONTEND_PORT$asset")
      asset_type=$(curl -sI "http://127.0.0.1:$FRONTEND_PORT$asset" | tr -d '\r' \
        | awk -F': ' 'tolower($1)=="content-type"{print $2}')
    fi
    echo "      / -> $code；app 挂载点=$( [[ "$has_app" -gt 0 ]] && echo yes || echo no )；首个 JS ${asset:-（无）} -> $asset_code ($asset_type)"
    login_code=$(curl -s -o "$NULL_DEV" -w '%{http_code}' -X POST \
      "http://127.0.0.1:$FRONTEND_PORT/api/auth/login" -H 'Content-Type: application/json' \
      -d "{\"username\":\"$DRILL_USERNAME\",\"password\":\"$DRILL_PASSWORD\"}")
    echo "      nginx /api 代理登录 -> $login_code（经代理能登录，说明 upstream 与转发正确）"

    set +e
    fe_out=$(python scripts/ask_once.py "http://127.0.0.1:$FRONTEND_PORT" "$TOKEN" "$ASK_KB" "$ASK_Q_PROXY")
    fe_rc=$?
    set -e
    echo "$fe_out" | sed 's/^/      /'
    # 直连首帧 vs 经 nginx 首帧：差一个数量级就说明代理在攒包（proxy_buffering 没关）
    direct_ms=$(printf '%s' "$ask_out" | python -c 'import json,sys;print(json.load(sys.stdin).get("first_frame_ms") or -1)' 2>/dev/null || echo -1)
    proxy_ms=$(printf '%s' "$fe_out" | python -c 'import json,sys;print(json.load(sys.stdin).get("first_frame_ms") or -1)' 2>/dev/null || echo -1)
    echo "      首帧耗时：直连 ${direct_ms}ms / 经 nginx ${proxy_ms}ms"
    fe_type=$(printf '%s' "$fe_out" | python -c 'import json,sys;print(json.load(sys.stdin).get("answer_type"))' 2>/dev/null || echo "?")
    if [[ "$fe_type" != "knowledge_base" ]]; then
      echo "      （本题落到 $fe_type：只验了「经代理仍然边生成边下发」，检索路径由 [5/7] 覆盖）"
    fi
    if [[ $fe_rc -ne 0 ]]; then
      echo "      ✗ 经 nginx 的 SSE 流不完整（退出码 $fe_rc）：旧镜像的 nginx 配置可能缺 SSE 相关项"
    elif [[ "$proxy_ms" == "-1" || $proxy_ms -gt 5000 ]]; then
      echo "      ！经 nginx 首帧 ${proxy_ms}ms 偏慢，检查旧镜像 nginx.conf 是否缺 proxy_buffering off"
    else
      echo "      ✓ 经 nginx 的 SSE 正常（未缓冲）"
    fi

    # 安全头差异：旧镜像的 nginx.conf 很可能落后（DEPLOY-3 时就发现过仓库比镜像新）
    hdrs() { curl -sI "$1" | tr -d '\r' | cut -d: -f1 | tr 'A-Z' 'a-z' | sort -u; }
    fe_hdrs=$(hdrs "http://127.0.0.1:$FRONTEND_PORT/")
    echo "      旧前端安全头：CSP=$(grep -qx content-security-policy <<<"$fe_hdrs" && echo yes || echo no) HSTS=$(grep -qx strict-transport-security <<<"$fe_hdrs" && echo yes || echo no) XFO=$(grep -qx x-frame-options <<<"$fe_hdrs" && echo yes || echo no)"
    if curl -fsS -o "$NULL_DEV" "http://127.0.0.1:8080/" 2>/dev/null; then
      prod_hdrs=$(hdrs "http://127.0.0.1:8080/")
      echo "      当前前端安全头：CSP=$(grep -qx content-security-policy <<<"$prod_hdrs" && echo yes || echo no) HSTS=$(grep -qx strict-transport-security <<<"$prod_hdrs" && echo yes || echo no) XFO=$(grep -qx x-frame-options <<<"$prod_hdrs" && echo yes || echo no)"
    fi
  fi
  docker rm -f "$DRILL_FRONTEND" >/dev/null 2>&1 || true
  docker network disconnect "$DRILL_NET" "$DRILL_BACKEND" >/dev/null 2>&1 || true
  docker network rm "$DRILL_NET" >/dev/null 2>&1 || true
fi

# ---------- 7. 停机行为：回滚会丢掉优雅停机 ----------
echo ""
echo "[7/7] 停机行为（P0-4 的优雅停机是 11-02 之后的镜像才有，回滚镜像没有）"
old_cmd=$(docker image inspect "$ROLLBACK_IMAGE" --format '{{join .Config.Cmd " "}}')
echo "      回滚镜像启动命令: $old_cmd"
if [[ "$old_cmd" != *"--timeout-graceful-shutdown"* ]]; then
  echo "      → 没有 --timeout-graceful-shutdown：P0-4 的 drain 与「停机窗口内 503 + Retry-After」都不存在"
fi

if [[ "$SKIP_QA" == "1" ]]; then
  echo "      ！没有可问的库，实时停机测试跳过（上面的启动命令差异仍是确定结论）"
else

# 必须先把演练 Redis 清掉：同一问题第二次问会命中语义缓存，几毫秒就返回，
# 「停机时有没有进行中的流」这个前提就不成立了（第一次跑就踩到，测出 2s 干净退出）。
docker exec -e REDISCLI_AUTH="$(cat "$SECRETS/redis_password")" "$DRILL_REDIS" \
  redis-cli --no-auth-warning FLUSHDB >/dev/null 2>&1 || true
# 用 curl 直接抓 SSE 并在**收到首帧后立刻**停机：早先版本是 sleep 3 再停，
# 而这道题 2~4 秒就答完了，多次跑下来停的都是「空连接」，结论忽 0 忽 137。
python -c "import json,sys;json.dump({'question':sys.argv[1],'kb_ids':[sys.argv[2]]},open(sys.argv[3],'w',encoding='utf-8'),ensure_ascii=False)" \
  "$ASK_Q_STOP" "$ASK_KB" "$(winpath "$WORK/stop_body.json")"
stream_file="$WORK/stream_at_stop.txt"
: > "$stream_file"
(set +e; curl -sS -N -X POST "http://127.0.0.1:$DRILL_PORT/api/chat/stream" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary "@$(winpath "$WORK/stop_body.json")" > "$stream_file" 2>&1) &
curl_pid=$!
for _ in $(seq 1 80); do [[ -s "$stream_file" ]] && break; sleep 0.25; done
if [[ ! -s "$stream_file" ]]; then
  echo "      ！20s 内没收到首帧，本次停机测试不成立（跳过）"
else
  sleep 0.3   # 只等首帧落地就停：多等 1s 这道题就答完了，测到的是「收尾之后」而不是「生成之中」
  t_stop=$(date +%s)
  docker stop "$DRILL_BACKEND" >/dev/null 2>&1 || true
  t_stop=$(( $(date +%s) - t_stop ))
  wait $curl_pid 2>/dev/null || true
  code=$(docker inspect "$DRILL_BACKEND" --format '{{.State.ExitCode}}' 2>/dev/null || echo "?")
  oom=$(docker inspect "$DRILL_BACKEND" --format '{{.State.OOMKilled}}' 2>/dev/null || echo "?")
  closed=$(docker logs "$DRILL_BACKEND" 2>&1 | grep -c "Application shutdown complete" || true)
  bytes=$(wc -c < "$stream_file" | tr -d '[:space:]')
  has_end=$(grep -c '"type": *"end"' "$stream_file" || true)
  echo "      docker stop 耗时 ${t_stop}s，ExitCode=$code，OOMKilled=$oom，正常收尾日志=$([[ "$closed" -gt 0 ]] && echo yes || echo no)"
  echo "      客户端收到 ${bytes} 字节，end 帧 $([[ "$has_end" -gt 0 ]] && echo 有 || echo 无)"
  if [[ "$has_end" == "0" ]]; then
    echo "      ✗ 生成中被停：客户端拿不到 end 帧，用户看到一段没有收尾的残答（ExitCode=$code）"
    echo "        日志若停在 'Waiting for connections to close'，就是 P0-4 要解决的那个形态"
  elif [[ "$code" == "137" ]]; then
    echo "      ！退出码是 137（SIGKILL），但这次回答已经写完（有 end 帧）——停机撞在收尾之后"
    echo "        优雅停机是**能力缺失**，不是每次都能撞上生成中的流；要复现就重跑"
  else
    echo "      ✓ 这次收尾正常（停机时流已结束）"
  fi
fi
fi   # SKIP_QA（实时停机测试）

# ---------- 汇总 ----------
t_all=$(( $(date +%s) - t0 ))
echo ""
echo "== 结论 =="
echo "回滚镜像在「已迁移到 $db_rev 的库」上：启动 ${t_ready}s 到 ready，登录与问答见上。"
echo "全流程 ${t_all}s。"
echo "仍需人工判断：答案是否真的对（脚本只报长度/引用数）、生产 Milvus/MinIO 是否被写（本脚本只读）。"
