#!/usr/bin/env bash
# =============================================================================
# Docker Secrets 生成脚本（W2-11）—— Bash 版，与 gen_secrets.ps1 等价
# 用法：
#   ./scripts/gen_secrets.sh                    # 随机生成（已存在的文件跳过）
#   ./scripts/gen_secrets.sh --force            # 覆盖重生成（慎用）
#   ./scripts/gen_secrets.sh --from-env .env.prod   # 从现有 env 导出
# =============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="${PROJECT_ROOT}/secrets"
FORCE=0
FROM_ENV=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --from-env) FROM_ENV="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

# secret 文件名|来源 env 键|随机长度（0 = 固定 24）
MANIFEST=(
  "postgres_password|POSTGRES_PASSWORD|24"
  "minio_root_user|MINIO_ROOT_USER|24"
  "minio_root_password|MINIO_ROOT_PASSWORD|24"
  "redis_password|REDIS_PASSWORD|24"
  "secret_key|SECRET_KEY|48"
  "admin_key|ADMIN_KEY|32"
  "search_api_key|SEARCH_API_KEY|24"
  "metrics_token|METRICS_TOKEN|32"
  "grafana_admin_password|GF_SECURITY_ADMIN_PASSWORD|24"
)

declare -A ENV_VALUES=()
if [[ -n "${FROM_ENV}" ]]; then
  ENV_PATH="${PROJECT_ROOT}/${FROM_ENV}"
  [[ -f "${ENV_PATH}" ]] || { echo "未找到 env 文件: ${ENV_PATH}"; exit 1; }
  while IFS='=' read -r k v; do
    [[ "${k}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    v="${v%%#*}"; v="${v%"${v##*[! ]}"}"; v="${v#\"}"; v="${v%\"}"
    ENV_VALUES["${k}"]="${v}"
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "${ENV_PATH}")
  echo "导出模式：从 ${FROM_ENV} 读取现有凭据（不轮换）"
else
  echo "随机生成模式：生成新凭据（即完成 W0-2 轮换）"
fi

mkdir -p "${SECRETS_DIR}"

for entry in "${MANIFEST[@]}"; do
  IFS='|' read -r name env_key len <<< "${entry}"
  target="${SECRETS_DIR}/${name}"
  if [[ -f "${target}" && "${FORCE}" -eq 0 ]]; then
    echo "  [跳过] ${name}（已存在，--force 覆盖）"
    continue
  fi
  if [[ -n "${FROM_ENV}" && -n "${ENV_VALUES[${env_key}]:-}" ]]; then
    value="${ENV_VALUES[${env_key}]}"
  else
    value="$(head -c 200 /dev/urandom | tr -dc 'A-Za-z0-9#%^+-_' | head -c "${len}")"
  fi
  printf '%s' "${value}" > "${target}"   # 无换行写入（容器侧 cat 读取）
  chmod 600 "${target}"
  echo "  [生成] ${name}"
done

echo ""
echo "完成。后续步骤："
echo "  1. 从 .env.prod 删除已 secrets 化的凭据键（POSTGRES_PASSWORD/MINIO_ROOT_*/REDIS_PASSWORD/SECRET_KEY/ADMIN_KEY/SEARCH_API_KEY/GF_SECURITY_ADMIN_PASSWORD）"
echo "  2. ./scripts/start-prod.sh（重启后端前自动执行 alembic 迁移）"
echo "  3. Prometheus 将以 /run/secrets/METRICS_TOKEN 抓取 /metrics（W2-12）"
