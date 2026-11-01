# 每日一致性巡检（只读，Windows 计划任务入口）。
#
# 与 nightly_consistency.sh 等价：用一次性容器跑 scripts/consistency_audit.py，
# 复用 compose 的 env_file / environment / 网络，凭据不落到宿主机。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\nightly_consistency.ps1
#   $env:AUDIT_DIR='D:\logs\rag-audit'; .\scripts\nightly_consistency.ps1 --skip-minio
#
# 退出码：0 一致 / 1 发现差异 / 2 巡检未完整执行（供计划任务与监控消费）

# 计划任务里没有交互终端：不设 UTF-8 会把中文输出打成乱码，日志就没法看了
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }
$env:PYTHONIOENCODING = 'utf-8'
# Git Bash 会把 -v 的容器路径改写成 Windows 路径；统一关掉路径转换
$env:MSYS_NO_PATHCONV = '1'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$OutDir = if ($env:AUDIT_DIR) { $env:AUDIT_DIR } else { Join-Path $Root 'backups\audit' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd'
$LogPath = Join-Path $OutDir "consistency_$Stamp.log"
$JsonPath = "/audit/consistency_$Stamp.json"

Write-Host "[nightly] 一致性巡检开始 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  输出目录 $OutDir"

# 用 cmd 做重定向而不是 PowerShell 的 2>&1：
# PS 会把 docker 的 stderr（进度条/警告）包装成 NativeCommandError 塞进日志，
# 真正的巡检输出反而被淹没；cmd 只透传字节，退出码从 $LASTEXITCODE 取。
# -T：不分配 TTY（计划任务里没有终端，否则报 "the input device is not a TTY"）
$argLine = ($args | ForEach-Object { "`"$_`"" }) -join ' '
$cmd = 'cmd /c "docker compose --env-file .env.prod run --rm --no-deps -T ' +
    "-v `"$OutDir`":/audit backend python scripts/consistency_audit.py " +
    "--json-out $JsonPath $argLine 1> `"$LogPath`" 2>&1" + '"'
cmd /c $cmd
$code = $LASTEXITCODE

Get-Content $LogPath -Encoding UTF8 | Select-Object -Last 30 | Write-Host

Write-Host "[nightly] 结束 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  exit_code=$code  json=$OutDir\consistency_$Stamp.json"
if ($code -eq 2) {
    Write-Host "[nightly] 巡检未完整执行（采集失败），请先确认 Postgres/Milvus/MinIO/Redis 可达" -ForegroundColor Red
}
exit $code
