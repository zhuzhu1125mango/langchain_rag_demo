# =============================================================================
# Docker Secrets 生成脚本（W2-11）
# -----------------------------------------------------------------------------
# 用途：在宿主机 ./secrets/ 目录下生成 prod compose 所需的全部凭据文件。
# 两种模式：
#   1. 随机生成（默认）：SECRET_KEY/ADMIN_KEY/METRICS_TOKEN 等用加密随机值，
#      数据库密码等同样随机生成 —— 即完成 W0-2 凭据轮换
#   2. 导出存量：-FromEnv .env.prod —— 把 .env.prod 中的现有凭据值导出到
#      secrets 文件（不轮换，用于先行切换部署形态）
# 用法：
#   pwsh ./scripts/gen_secrets.ps1                 # 随机生成（已存在的文件跳过）
#   pwsh ./scripts/gen_secrets.ps1 -Force          # 覆盖重生成（慎用，会使现有容器密码失效）
#   pwsh ./scripts/gen_secrets.ps1 -FromEnv .env.prod   # 从现有 env 导出
# 生成后：docker compose -f docker-compose.yml --env-file .env.prod up -d
# 注意：./secrets/ 已被 .gitignore 忽略，严禁入库或提交。
# =============================================================================

param(
    [string]$FromEnv = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$secretsDir = Join-Path $projectRoot "secrets"

# secret 文件名 -> (来源 env 键 | "random")
$manifest = [ordered]@{
    "postgres_password"      = "POSTGRES_PASSWORD"
    "minio_root_user"        = "MINIO_ROOT_USER"
    "minio_root_password"    = "MINIO_ROOT_PASSWORD"
    "redis_password"         = "REDIS_PASSWORD"
    "secret_key"             = "SECRET_KEY"
    "admin_key"              = "ADMIN_KEY"
    "search_api_key"         = "SEARCH_API_KEY"
    "metrics_token"          = "METRICS_TOKEN"
    "grafana_admin_password" = "GF_SECURITY_ADMIN_PASSWORD"
}

# 随机生成模式下的长度（字符）；SECRET_KEY 走强度校验需 >=32 且含 3 类字符
$randomLengths = @{
    "secret_key"             = 48
    "admin_key"              = 32
    "metrics_token"          = 32
}

function New-RandomSecret {
    param([int]$Length)
    # 大小写 + 数字 + 特殊字符，保证通过 validate_secret_key 的 3 类字符校验
    $chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789#%^+-_"
    $bytes = New-Object byte[] $Length
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

$envValues = @{}
if ($FromEnv) {
    $envPath = Join-Path $projectRoot $FromEnv
    if (-not (Test-Path $envPath)) { throw "未找到 env 文件: $envPath" }
    Get-Content $envPath | ForEach-Object {
        if ($_ -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$") {
            $envValues[$Matches[1]] = $Matches[2] -replace "\s*#.*$", "" -replace '^"(.*)"$', '$1'
        }
    }
    Write-Host "导出模式：从 $FromEnv 读取现有凭据（不轮换）"
}
else {
    Write-Host "随机生成模式：生成新凭据（即完成 W0-2 轮换）"
}

if (-not (Test-Path $secretsDir)) { New-Item -ItemType Directory -Path $secretsDir | Out-Null }

foreach ($name in $manifest.Keys) {
    $target = Join-Path $secretsDir $name
    if ((Test-Path $target) -and -not $Force) {
        Write-Host "  [跳过] $name（已存在，-Force 覆盖）"
        continue
    }

    $value = $null
    $envKey = $manifest[$name]
    if ($FromEnv -and $envValues.ContainsKey($envKey) -and $envValues[$envKey]) {
        $value = $envValues[$envKey]
    }
    elseif ($randomLengths.ContainsKey($name)) {
        $value = New-RandomSecret -Length $randomLengths[$name]
    }
    else {
        $value = New-RandomSecret -Length 24
    }

    # 无换行写入（容器侧 cat 读取，尾随换行会并入密码）
    [System.IO.File]::WriteAllText($target, $value)
    Write-Host "  [生成] $name"
}

Write-Host ""
Write-Host "完成。后续步骤："
Write-Host "  1. 从 .env.prod 删除已 secrets 化的凭据键（POSTGRES_PASSWORD/MINIO_ROOT_*/REDIS_PASSWORD/SECRET_KEY/ADMIN_KEY/SEARCH_API_KEY/GF_SECURITY_ADMIN_PASSWORD）"
Write-Host "  2. ./scripts/start-prod.sh（重启后端前自动执行 alembic 迁移）"
Write-Host "  3. Prometheus 将以 /run/secrets/METRICS_TOKEN 抓取 /metrics（W2-12）"
