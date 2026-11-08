#!/usr/bin/env bash
# P1-4 / #31：本地或服务器上的夜间端到端探针。
#
# 为什么不放进 GitHub Actions：
#   托管 runner 起不了 11 个容器 + 5.6GB 模型（内存、磁盘、时间都不够），
#   硬塞进去的结果只有一个——那一步永远 skip，而 workflow 仍然是绿的。
#   「看起来接了其实没跑」比没有这条检查更糟，所以这里明确放在 CI 之外，
#   由本机/服务器上的定时任务执行。
#
# 用法（二选一鉴权）：
#   E2E_TOKEN=<管理员 JWT>    ./scripts/nightly_e2e.sh
#   E2E_ADMIN_KEY=<X-Admin-Key 全局密钥> ./scripts/nightly_e2e.sh
#
# 参数（可选）：
#   E2E_BASE  默认 http://127.0.0.1:8001（**直连后端**）。
#             不要填 8080/nginx：nginx 只代理 /api/，/health/* 会落到 SPA 的
#             try_files 返回 index.html —— HTTP 200 + text/html，看起来成功，
#             其实什么都没测。脚本会校验 content-type 来挡这种情况。
#   E2E_LOG   默认 <repo>/logs/nightly-e2e.log（自动追加，保留最近 500 行）
#
# 退出码：0 探针通过 · 1 探针失败（503/业务不通） · 2 配置缺失（没跑） · 3 网络不可达
#
# 挂定时任务：
#   Linux/macOS:  crontab -e
#     30 2 * * * cd /path/to/repo && E2E_TOKEN=... ./scripts/nightly_e2e.sh >> logs/cron.log 2>&1
#   Windows: 计划任务 → 每天 02:30 → 程序选 bash.exe，参数为脚本绝对路径（见 deployment.md）
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
E2E_BASE="${E2E_BASE:-http://127.0.0.1:8001}"
E2E_LOG="${E2E_LOG:-$ROOT_DIR/logs/nightly-e2e.log}"
PYTHON_BIN="$(command -v python || command -v python3 || true)"

mkdir -p "$(dirname "$E2E_LOG")"

stamp="$(date '+%Y-%m-%d %H:%M:%S')"

if [[ -z "${E2E_TOKEN:-}" && -z "${E2E_ADMIN_KEY:-}" ]]; then
  echo "[$stamp] SKIP 未配置 E2E_TOKEN / E2E_ADMIN_KEY，探针未执行" | tee -a "$E2E_LOG" >&2
  exit 2
fi

auth=()
if [[ -n "${E2E_TOKEN:-}" ]]; then
  auth=(-H "Authorization: Bearer $E2E_TOKEN")
else
  auth=(-H "X-Admin-Key: $E2E_ADMIN_KEY")
fi

# 不用临时文件接响应体。Git Bash 的 mktemp 返回带盘符的路径，而部分环境里
# `rm -f` 会被安全钩子拒绝（返回 1），配上 set -e 就直接把脚本带走 ——
# 实测（2026-11-08）：退出码 1、日志一个字都没写，看起来像「探针失败」。
resp="$(curl -s -m 120 -w $'\n%{http_code} %{content_type}' "${auth[@]}" "$E2E_BASE/health/e2e" || true)"
code="$(printf '%s' "$resp" | tail -n 1 | awk '{print $1}')"
ctype="$(printf '%s' "$resp" | tail -n 1 | awk '{print $2}')"
body="$(printf '%s' "$resp" | sed '$d')"
: "${code:=000}"

# 只保留最近 500 行，避免日志无限长（无人看的日志等于没有）
trim_log() {
  if [[ -f "$E2E_LOG" ]] && (( $(wc -l < "$E2E_LOG") > 500 )); then
    tail -n 500 "$E2E_LOG" > "$E2E_LOG.tmp" && mv "$E2E_LOG.tmp" "$E2E_LOG"
  fi
}

case "$code" in
  200)
    # 200 但 content-type 不是 JSON，说明请求被 nginx 的 SPA fallback 接走了。
    # 这是「假绿」最典型的样子：状态码对、内容是 index.html。
    if [[ "$ctype" != application/json* ]]; then
      echo "[$stamp] MISROUTED $code $ctype —— 拿到的是 index.html，不是探针结果（E2E_BASE 要指向后端端口，不要经 nginx）" \
        | tee -a "$E2E_LOG" >&2
      trim_log
      exit 3
    fi
    echo "[$stamp] OK $body" | tee -a "$E2E_LOG"
    trim_log
    exit 0
    ;;
  000)
    echo "[$stamp] UNREACHABLE $E2E_BASE（服务没起或端口不对）" | tee -a "$E2E_LOG" >&2
    trim_log
    exit 3
    ;;
  404)
    # 端点被显式关闭（HEALTH_E2E_ENABLED=false）与「地址写错」都是 404，
    # 但两者的处置完全不同，所以把响应体一起记下来。
    echo "[$stamp] DISABLED_OR_MISSING $code $body" | tee -a "$E2E_LOG" >&2
    trim_log
    exit 1
    ;;
  *)
    echo "[$stamp] FAIL $code $body" | tee -a "$E2E_LOG" >&2
    trim_log
    exit 1
    ;;
esac
