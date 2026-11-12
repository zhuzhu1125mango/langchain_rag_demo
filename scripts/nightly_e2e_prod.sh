#!/usr/bin/env bash
# 生产夜间探针（宿主侧包装）：给 `scripts/nightly_e2e.sh` 补上「凭据从哪来」。
#
# 为什么要这一层：
#   `nightly_e2e.sh` 要求显式传 E2E_TOKEN 或 E2E_ADMIN_KEY，这是对的——运维脚本
#   不该自己去猜密钥。但定时任务没有「人」来提供，于是必须有一个明确的地方负责取。
#   把密钥写进 crontab 或 .env 的又一处分身，正是「旧 Key 躺在备份里」那类问题的来源。
#
# 这里的选择是：**每次运行时从 backend 容器内的 settings 现取**（生产凭据的唯一真身），
# 只在本次进程的内存与环境变量里存在，不落盘、不打印、不新增副本。
#
# 用法：
#   ./scripts/nightly_e2e_prod.sh            # 正常跑，日志追加到 logs/nightly-e2e.log
#   E2E_BASE=http://127.0.0.1:8001 ./scripts/nightly_e2e_prod.sh
#
# 退出码沿用 nightly_e2e.sh：0 通过 · 1 探针失败 · 2 配置缺失 · 3 不可达
# 额外：容器未运行/不健康 → 3（不把「docker 没开」记成「业务不通」）
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$ROOT_DIR/logs/nightly-e2e.log"
CONTAINER="${E2E_CONTAINER:-backend-prod}"
mkdir -p "$(dirname "$LOG")"

stamp="$(date '+%Y-%m-%d %H:%M:%S')"

# 1. 容器必须健康。否则后面所有失败都是「环境没起」而不是「业务坏了」——
#    这类误判会让人开始忽略告警，比不告警更糟。
health="$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "absent")"
if [[ "$health" != "healthy" ]]; then
  echo "[$stamp] SKIP 容器 $CONTAINER 状态=$health，探针未执行" | tee -a "$LOG" >&2
  exit 3
fi

# 2. 现取凭据（只看长度，绝不回显）。取不到就是配置问题，明确报 2，别让它变成「探针失败」。
key_len="$(docker exec "$CONTAINER" python -c \
  'from src.config import settings; print(len(settings.security.ADMIN_KEY or ""))' 2>/dev/null || echo 0)"
if [[ "$key_len" == "0" ]]; then
  echo "[$stamp] SKIP 容器内未配置 ADMIN_KEY，探针未执行" | tee -a "$LOG" >&2
  exit 2
fi

E2E_KEY="$(docker exec "$CONTAINER" python -c \
  'from src.config import settings; print(settings.security.ADMIN_KEY or "")' 2>/dev/null)"
if [[ -z "$E2E_KEY" ]]; then
  echo "[$stamp] SKIP 读取 ADMIN_KEY 失败，探针未执行" | tee -a "$LOG" >&2
  exit 2
fi

# 3. 交给原脚本：它负责 nginx 假绿识别、content-type 校验、日志裁剪
E2E_BASE="${E2E_BASE:-http://127.0.0.1:8001}" \
E2E_ADMIN_KEY="$E2E_KEY" \
E2E_LOG="$LOG" \
  "$ROOT_DIR/scripts/nightly_e2e.sh"
code=$?

unset E2E_KEY
# 用收尾时刻的时间戳：沿用开头那个 stamp 会让这行排在它自己的结果之后
# （撞限流时脚本会多等 65s，日志里就出现「16:01 OK」在「16:00 exit」上面）。
echo "[$(date '+%Y-%m-%d %H:%M:%S')] wrapper exit=$code" >> "$LOG"
exit $code
