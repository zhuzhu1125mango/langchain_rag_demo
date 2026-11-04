#!/usr/bin/env bash
# 恢复演练（runbook §7.1）：在隔离环境把备份真正恢复一次，量出 RTO。
#
# 为什么需要它：备份存在 ≠ 能恢复。演练的价值不在于「跑通了」，而在于**让它失败**——
# 2026-11-04 首次演练就发现 backups/ 里全是 pg_dump -Fc 的 .dump，而 restore.sh 当时
# 只会 gunzip|psql，按 runbook 恢复会直接失败。
#
# 关键设计：**真的调用 scripts/restore.sh**（而不是在演练脚本里另写一套恢复逻辑）。
# 否则演练验证的是演练脚本，不是运维真正会敲的那条命令。
# 因为宿主机没有 psql/pg_restore，所以把脚本和备份一起拷进临时 PG 容器里执行。
#
# 安全边界（刻意的）：
#   - 只创建/删除名字以 drill- 开头的容器与卷；不碰任何生产容器与卷
#   - 从生产**读**备份文件，不写入生产库；不 stop 生产 backend
#   - 端口用 5547（避开生产 5434 / 开发 5433）
#
# 用法：
#   ./scripts/restore_drill.sh                       # 用 backups/ 里最新的产物
#   ./scripts/restore_drill.sh backups/xxx.dump      # 指定产物
#   KEEP=1 ./scripts/restore_drill.sh                # 保留演练容器，便于手工排查
set -euo pipefail

# Windows Git Bash（MSYS2）会把以 / 开头的参数当成宿主机路径改写：
# `docker exec ... bash /tmp/restore.sh` 会被塞成 C:/Users/.../restore.sh，报 No such file。
# runbook 指定的执行环境正是 Git Bash，所以这里显式关掉路径转换。
export MSYS_NO_PATHCONV=1

PG_IMAGE="${PG_IMAGE:-postgres:16.14}"
DRILL_PG="drill-pg"
DRILL_VOL="drill_pg_data"
DRILL_PORT="${DRILL_PORT:-5547}"
DRILL_DB="app_drill"
DRILL_USER="drill_user"
DRILL_PW="drill_pw"          # 临时库，仅监听 127.0.0.1，演练结束即销毁
KEEP="${KEEP:-0}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PG_FILE="${1:-}"
if [[ -z "$PG_FILE" ]]; then
  PG_FILE="$(ls -t backups/*.dump backups/*.sql.gz 2>/dev/null | head -1 || true)"
fi
[[ -n "$PG_FILE" && -f "$PG_FILE" ]] || { echo "找不到备份产物；请显式传参" >&2; exit 1; }

echo "== 恢复演练 =="
echo "产物: $PG_FILE ($(du -h "$PG_FILE" | cut -f1))"
echo ""

cleanup() {
  if [[ "$KEEP" == "1" ]]; then
    echo "（KEEP=1，保留 $DRILL_PG 与 $DRILL_VOL 供排查）"
    return
  fi
  docker rm -f "$DRILL_PG" >/dev/null 2>&1 || true
  docker volume rm "$DRILL_VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ---------- 1. 起隔离 PG ----------
t0=$(date +%s)
docker rm -f "$DRILL_PG" >/dev/null 2>&1 || true
docker volume rm "$DRILL_VOL" >/dev/null 2>&1 || true
docker run -d --name "$DRILL_PG" \
  -e "POSTGRES_PASSWORD=$DRILL_PW" -e "POSTGRES_DB=$DRILL_DB" -e "POSTGRES_USER=$DRILL_USER" \
  -p "127.0.0.1:$DRILL_PORT:5432" -v "$DRILL_VOL:/var/lib/postgresql/data" \
  "$PG_IMAGE" >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$DRILL_PG" pg_isready -U "$DRILL_USER" -d "$DRILL_DB" >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$DRILL_PG" pg_isready -U "$DRILL_USER" -d "$DRILL_DB" >/dev/null || {
  echo "临时 PG 未就绪" >&2; exit 1; }
t_pg_up=$(( $(date +%s) - t0 ))
echo "[1/4] 空库就绪（含容器启动）: ${t_pg_up}s"

# ---------- 2. 调用真实 restore.sh ----------
docker cp "$PG_FILE" "$DRILL_PG:/tmp/backup.bin" >/dev/null
docker cp scripts/restore.sh "$DRILL_PG:/tmp/restore.sh" >/dev/null
t1=$(date +%s)
docker exec \
  -e POSTGRES_HOST=127.0.0.1 -e POSTGRES_PORT=5432 \
  -e "POSTGRES_DB=$DRILL_DB" -e "POSTGRES_USER=$DRILL_USER" -e "POSTGRES_PASSWORD=$DRILL_PW" \
  -e PG_FILE=/tmp/backup.bin -e TARGET_EMPTY=1 \
  "$DRILL_PG" bash /tmp/restore.sh
t_restore=$(( $(date +%s) - t1 ))
echo "[2/4] restore.sh 执行完成: ${t_restore}s"

# ---------- 3. 一致性抽查 ----------
tables=$(docker exec "$DRILL_PG" psql -U "$DRILL_USER" -d "$DRILL_DB" -tAc \
  "select count(*) from information_schema.tables where table_schema='public'" | tr -d '[:space:]')
counts=$(docker exec "$DRILL_PG" psql -U "$DRILL_USER" -d "$DRILL_DB" -tAc \
  "select (select count(*) from users), (select count(*) from knowledge_bases),
          (select count(*) from documents), (select count(*) from request_traces)" 2>/dev/null \
  | tr -d '[:space:]')
echo "[3/4] 还原后 表数=$tables  行数(users/kb/docs/traces)=$counts"

prod_counts=$(docker exec postgres-prod psql -U prod_user -d app_prod -tAc \
  "select (select count(*) from users), (select count(*) from knowledge_bases),
          (select count(*) from documents), (select count(*) from request_traces)" 2>/dev/null \
  | tr -d '[:space:]' || echo "n/a")
echo "      生产侧=$prod_counts（备份是 T 时刻快照，之后新增的数据不在其中）"

# ---------- 4. RTO ----------
t_all=$(( $(date +%s) - t0 ))
echo "[4/4] 演练总耗时（RTO 实测）= ${t_all}s"
echo ""
echo "结论：PG ${tables} 张表、${t_restore}s 导入完成。"
echo "仍需人工确认（本脚本不覆盖）：MinIO 原件回灌、Milvus 重建、应用 /health/ready、"
echo "知识库问答抽查、权限隔离用例。"
