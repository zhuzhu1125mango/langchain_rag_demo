#Requires -Version 7.0
<#
.SYNOPSIS
    回收 Docker Desktop 里 containerd 遗留的快照层（可逆，带闸口校验）

.DESCRIPTION
    背景（2026-11-11 实测）：Docker Desktop 曾启用 containerd 镜像存储，关闭后 docker
    改用 overlay2（docker info: Storage Driver: overlay2），但 containerd 的
    io.containerd.snapshotter.v1.overlayfs 原地遗留了约 48GB 快照层，docker system df
    完全看不到（两套层存储并存 → 磁盘占用翻倍）。

    本脚本把该目录**改名**（不是删除），重启 Docker Desktop 验证 11 个服务与镜像均正常，
    确认无误才删除。任何一步异常自动回滚，零损失。

    关键顺序（踩过的坑）：不能在 `docker desktop stop` 之后改名——Docker Desktop 停止时
    WSL 发行版会关闭、docker_data.vhdx 卸载，/mnt/docker-desktop-disk 不存在，mv 必然失败。
    必须：停容器（VM 仍运行）→ 改名 → 重启 VM 让 containerd 重新加载 → 验证。

.PARAMETER Force
    跳过交互确认。计划任务/无人值守必须带。

.PARAMETER SkipBackupCheck
    跳过备份闸口（危险：脚本会二次确认，除非同时给 -Force）。

.PARAMETER BackupMaxAgeHours
    备份闸口要求的最大时效，默认 24 小时。

.PARAMETER KeepStale
    验证通过后保留 .stale 目录不删除（只改名腾出 nothing，适合先观察几天）。

.PARAMETER Compact
    删除成功后接着调用 docker-compact.ps1 -Restart -StartStack 压缩 vhdx。

.PARAMETER ComposeFile
    覆盖 compose 文件路径，默认仓库根 docker-compose.yml。

.EXAMPLE
    .\scripts\containerd-cleanup.ps1

.EXAMPLE
    .\scripts\containerd-cleanup.ps1 -Force -Compact

.NOTES
    用法：./scripts/containerd-cleanup.ps1 [-Force] [-SkipBackupCheck]
          [-BackupMaxAgeHours 24] [-KeepStale] [-Compact]
    退出码：0 成功 / 1 环境错误 / 3 备份闸口未过 / 4 存在上次残留 .stale
            5 停容器失败 / 6 改名失败 / 7 验证失败（已回滚）/ 8 删除失败
#>

param(
    [switch]$Force,
    [switch]$SkipBackupCheck,
    [int]$BackupMaxAgeHours = 24,
    [switch]$KeepStale,
    [switch]$Compact,
    [string]$ComposeFile
)

$ErrorActionPreference = 'Stop'

# ==============================================================================
# Configuration
# ==============================================================================

$script:ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent
$script:RootDir = Split-Path $script:ScriptDir -Parent
$script:LogDir = Join-Path $script:RootDir 'backups/compact'
$script:LogFile = Join-Path $script:LogDir ("containerd-cleanup_{0}.log" -f (Get-Date -Format 'yyyyMMdd'))
$script:BackupDir = Join-Path $script:RootDir 'backups'
$script:ComposeFile = if ($ComposeFile) { $ComposeFile } else { Join-Path $script:RootDir 'docker-compose.yml' }
$script:EnvFile = Join-Path $script:RootDir '.env.prod'
$script:DistroName = 'docker-desktop'
$script:VhdxPath = Join-Path $env:LOCALAPPDATA 'Docker\wsl\disk\docker_data.vhdx'
$script:DaemonTimeoutSec = 180

# containerd 的 snapshotter 目录；.stale 后缀是可逆标记
$script:SnapshotterPath = '/mnt/docker-desktop-disk/data/desktop-containerd/daemon/io.containerd.snapshotter.v1.overlayfs'
$script:StalePath = "$($script:SnapshotterPath).stale"

# 回滚标记：一旦改名成功置 true，任何失败都据此回滚
$script:Renamed = $false

New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

# ==============================================================================
# Color Definitions & Logging
# ==============================================================================

# 不要写成 `{ "`e[$Code" }`：函数体里的裸 { } 是 ScriptBlock 表达式，
# 函数会返回 ScriptBlock 对象而不是字符串，插进 "${Bold}" 后满屏打印源码文本。
function Get-ANSI { param([string]$Code) "$([char]27)[$Code" }

$Reset = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "0m" } else { "" }
$Red = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "31m" } else { "" }
$Green = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "32m" } else { "" }
$Yellow = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "33m" } else { "" }
$Blue = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "34m" } else { "" }
$Cyan = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "36m" } else { "" }
$Bold = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "1m" } else { "" }

function Write-Info { Write-Host "${Blue}[INFO]${Reset} $args" }
function Write-OK { Write-Host "${Green}[OK]${Reset} $args" }
function Write-Warn { Write-Host "${Yellow}[WARN]${Reset} $args" }
function Write-Err { Write-Host "${Red}[ERROR]${Reset} $args" }
function Write-Step { param([string]$Step, [string]$Message) Write-Host "${Cyan}[$Step]${Reset} $Message" }

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Add-Content -Path $script:LogFile -Value $line -Encoding utf8
    switch ($Level) {
        'OK' { Write-OK $Message }
        'WARN' { Write-Warn $Message }
        'ERR' { Write-Err $Message }
        default { Write-Info $Message }
    }
}

# ==============================================================================
# Helpers
# ==============================================================================

function Invoke-Wsl {
    param([string]$Command)
    $out = & wsl.exe -d $script:DistroName -u root -- sh -c $Command 2>&1
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = @($out) }
}

function Test-DockerDaemon {
    $null = docker info --format '{{.ServerVersion}}' 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Wait-DockerDaemon {
    param([int]$TimeoutSec = 180)
    $elapsed = 0
    while ($elapsed -lt $TimeoutSec) {
        if (Test-DockerDaemon) { return $true }
        Start-Sleep -Seconds 3
        $elapsed += 3
    }
    return $false
}

function Get-VhdxSizeGB {
    if (-not (Test-Path -LiteralPath $script:VhdxPath)) { return 0 }
    return [math]::Round((Get-Item -LiteralPath $script:VhdxPath).Length / 1GB, 2)
}

# 当前栈状态：容器总数、运行数、健康数、异常数
function Get-StackState {
    $rows = & docker compose -f $script:ComposeFile ps --format "{{.Name}}`t{{.State}}`t{{.Status}}" 2>$null
    $total = 0; $running = 0; $unhealthy = 0
    foreach ($row in @($rows)) {
        if (-not $row) { continue }
        $f = $row -split "`t"
        if ($f.Count -lt 3) { continue }
        $total++
        if ($f[1] -eq 'running') { $running++ }
        if ($f[2] -match 'unhealthy') { $unhealthy++ }
    }
    return [pscustomobject]@{ Total = $total; Running = $running; Unhealthy = $unhealthy }
}

function Get-ImageCount {
    $ids = & docker images -q 2>$null
    return @($ids | Where-Object { $_ } | Sort-Object -Unique).Count
}

function Get-ComposeArgs {
    # 与 start-prod.ps1 保持一致：生产保留监控栈（--profile monitoring）
    $args = @('compose', '-f', $script:ComposeFile)
    if (Test-Path -LiteralPath $script:EnvFile) { $args += @('--env-file', $script:EnvFile) }
    $args += @('--profile', 'monitoring')
    return $args
}

function Start-Stack {
    $composeArgs = Get-ComposeArgs
    & docker @composeArgs start 2>&1 | Out-Null
    Start-Sleep -Seconds 10

    $state = Get-StackState
    if ($state.Running -lt $script:BaselineRunning) {
        Write-Log "compose start 后运行数 $($state.Running)/$($script:BaselineRunning)，退回 compose up -d" 'WARN'
        & docker @composeArgs up -d 2>&1 | Out-Null
        Start-Sleep -Seconds 15
    }
}

function Wait-StackHealthy {
    param([int]$TimeoutSec = 180)
    $elapsed = 0
    while ($elapsed -lt $TimeoutSec) {
        $state = Get-StackState
        if ($state.Running -ge $script:BaselineRunning -and $state.Unhealthy -eq 0) { return $state }
        Start-Sleep -Seconds 5
        $elapsed += 5
    }
    return (Get-StackState)
}

function Get-LatestBackupAge {
    # backup.sh 产物：backups/postgres_YYYYMMDD_HHMMSS.sql.gz 与 backups/minio_YYYYMMDD_HHMMSS/
    $pg = Get-ChildItem -Path $script:BackupDir -Filter 'postgres_*.sql.gz' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $minio = Get-ChildItem -Path $script:BackupDir -Filter 'minio_*' -Directory -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1

    $result = [pscustomobject]@{ PgAgeHours = $null; PgPath = ''; MinioAgeHours = $null; MinioPath = '' }
    if ($pg) {
        $result.PgAgeHours = [math]::Round(((Get-Date) - $pg.LastWriteTime).TotalHours, 1)
        $result.PgPath = $pg.FullName
    }
    if ($minio) {
        $result.MinioAgeHours = [math]::Round(((Get-Date) - $minio.LastWriteTime).TotalHours, 1)
        $result.MinioPath = $minio.FullName
    }
    return $result
}

# ==============================================================================
# Gates
# ==============================================================================

function Test-BackupGate {
    Write-Step "1/7" "闸口：备份时效检查（要求 ${BackupMaxAgeHours} 小时内）..."

    $age = Get-LatestBackupAge

    if ($SkipBackupCheck) {
        Write-Log "已跳过备份闸口（-SkipBackupCheck）—— 请自行确认数据可恢复" 'WARN'
        if (-not $Force) {
            $resp = Read-Host "${Bold}跳过备份检查继续? 输入 SKIP 确认:${Reset}"
            if ($resp -ne 'SKIP') { Write-Log "已取消" 'WARN'; exit 3 }
        }
        return $true
    }

    $ok = $true

    # 注意：不能用 `-not $age.PgAgeHours` 判空——0 在 PowerShell 里是 falsy，
    # 刚做完的备份年龄是 0.0 小时，会被误判成"找不到备份"（2026-11-11 踩过）。
    if ($null -eq $age.PgAgeHours) {
        Write-Log "backups/ 下找不到 postgres_*.sql.gz" 'ERR'
        $ok = $false
    } elseif ($age.PgAgeHours -gt $BackupMaxAgeHours) {
        Write-Log ("PostgreSQL 备份已 {0} 小时，超过 {1} 小时上限：{2}" -f $age.PgAgeHours, $BackupMaxAgeHours, $age.PgPath) 'ERR'
        $ok = $false
    } else {
        Write-Log ("PostgreSQL 备份 {0} 小时前（{1}）" -f $age.PgAgeHours, (Split-Path $age.PgPath -Leaf)) 'OK'
    }

    if ($null -eq $age.MinioAgeHours) {
        Write-Log "backups/ 下找不到 minio_* 目录" 'ERR'
        $ok = $false
    } elseif ($age.MinioAgeHours -gt $BackupMaxAgeHours) {
        Write-Log ("MinIO 备份已 {0} 小时，超过上限：{1}" -f $age.MinioAgeHours, $age.MinioPath) 'ERR'
        $ok = $false
    } else {
        Write-Log ("MinIO 备份 {0} 小时前（{1}）" -f $age.MinioAgeHours, (Split-Path $age.MinioPath -Leaf)) 'OK'
    }

    # Milvus 不在 backup.sh 覆盖范围内（脚本自己注明用 milvus-backup 单独做），只能提醒
    Write-Log "注意：Milvus 向量库不在 backup.sh 覆盖范围内，若需完整恢复能力请先用 milvus-backup 单独备份" 'WARN'

    if (-not $ok) {
        Write-Log "备份闸口未通过。先跑 bash scripts/backup.sh 生成新备份，或用 -BackupMaxAgeHours 放宽时效" 'ERR'
        Write-Log "仅评估风险但不备份可用 -SkipBackupCheck（不推荐）" 'ERR'
        return $false
    }

    Write-Log "备份闸口通过" 'OK'
    return $true
}

# ==============================================================================
# Main Steps
# ==============================================================================

function Confirm-Proceed {
    Write-Host ""
    Write-Log "即将：停容器 → 改名 containerd 快照目录 → 重启 Docker → 验证 → 删除" 
    Write-Log "预计回收约 48GB；任何一步失败会自动回滚" 'WARN'

    if ($Force) { return }
    if ([Console]::IsInputRedirected) {
        Write-Log "无交互终端且未指定 -Force，中止" 'ERR'
        exit 1
    }

    Write-Host ""
    $resp = Read-Host "${Bold}继续? [y/N]:${Reset}"
    if ($resp -notmatch '^[Yy]$') { Write-Log "已取消" 'WARN'; exit 0 }
}

function Stop-Containers {
    Write-Step "3/7" "停止 Compose 容器（VM 保持运行，数据盘保持挂载）..."

    $composeArgs = Get-ComposeArgs
    & docker @composeArgs stop 2>&1 | Out-Null
    $state = Get-StackState
    Write-Log "容器已停止（运行中 $($state.Running)/$($state.Total)）" 'OK'

    # 确认数据盘仍挂载——这是改名能成功的前提
    $df = Invoke-Wsl "df -h /mnt/docker-desktop-disk | tail -1"
    if ($df.ExitCode -ne 0 -or -not ($df.Output -match 'docker-desktop-disk')) {
        Write-Log "数据盘未挂载，无法改名。请确认 Docker Desktop 正在运行（不要先 stop）" 'ERR'
        return $false
    }
    Write-Log "数据盘已挂载：$(($df.Output -join ' ').Trim())" 'OK'
    return $true
}

function Rename-Snapshotter {
    Write-Step "4/7" "改名 snapshotter 目录（可逆操作，不删数据）..."

    $exists = Invoke-Wsl "test -d '$($script:SnapshotterPath)' && echo YES || echo NO"
    if (($exists.Output -join '') -notmatch 'YES') {
        Write-Log "找不到 $($script:SnapshotterPath)，可能已被处理过" 'ERR'
        return $false
    }

    $staleExists = Invoke-Wsl "test -d '$($script:StalePath)' && echo YES || echo NO"
    if (($staleExists.Output -join '') -match 'YES') {
        Write-Log "已存在 $($script:StalePath)，请先确认上次执行结果再重跑" 'ERR'
        return $false
    }

    $mv = Invoke-Wsl "mv '$($script:SnapshotterPath)' '$($script:StalePath)' && echo MOVED"
    if ($mv.ExitCode -ne 0 -or ($mv.Output -join '') -notmatch 'MOVED') {
        Write-Log "改名失败：$($mv.Output -join ' ')" 'ERR'
        return $false
    }

    $script:Renamed = $true
    Write-Log "已改名 → $(Split-Path $script:StalePath -Leaf)" 'OK'
    return $true
}

function Restart-Docker {
    Write-Step "5/7" "重启 Docker Desktop（让 containerd 重新加载）..."

    docker desktop stop 2>&1 | Out-Null
    Start-Sleep -Seconds 3
    docker desktop start 2>&1 | Out-Null

    if (-not (Wait-DockerDaemon -TimeoutSec $script:DaemonTimeoutSec)) {
        Write-Log "Docker 守护进程未在 $($script:DaemonTimeoutSec) 秒内就绪" 'ERR'
        return $false
    }
    Write-Log "Docker 已就绪" 'OK'
    return $true
}

function Test-AfterCleanup {
    Write-Step "6/7" "验证：镜像数量、容器健康状态..."

    Start-Stack | Out-Null
    $state = Wait-StackHealthy -TimeoutSec 180
    $images = Get-ImageCount

    Write-Log ("镜像 {0} 个（基线 {1}）" -f $images, $script:BaselineImages)
    Write-Log ("容器 运行 {0}/{1} 健康异常 {2}（基线运行 {3}）" -f $state.Running, $state.Total, $state.Unhealthy, $script:BaselineRunning)

    $ok = $true
    if ($images -lt $script:BaselineImages) {
        Write-Log "镜像数量减少，可能 containerd 快照仍被引用 —— 判定失败" 'ERR'
        $ok = $false
    }
    if ($state.Running -lt $script:BaselineRunning) {
        Write-Log "运行中的容器少于基线 —— 判定失败" 'ERR'
        $ok = $false
    }
    if ($state.Unhealthy -gt 0) {
        Write-Log "存在 unhealthy 容器 —— 判定失败" 'ERR'
        $ok = $false
    }

    if ($ok) { Write-Log "验证通过：镜像与容器均正常" 'OK' }
    return $ok
}

function Remove-Stale {
    Write-Step "7/7" "删除 .stale 目录..."

    if ($KeepStale) {
        Write-Log "保留 .stale（-KeepStale）。确认无误后手动删除：wsl -d $($script:DistroName) -u root -- rm -rf '$($script:StalePath)'" 'WARN'
        return $true
    }

    $rm = Invoke-Wsl "rm -rf '$($script:StalePath)' && echo REMOVED"
    if ($rm.ExitCode -ne 0 -or ($rm.Output -join '') -notmatch 'REMOVED') {
        Write-Log "删除失败：$($rm.Output -join ' ')。数据仍可回滚（.stale 还在）" 'ERR'
        return $false
    }
    Write-Log "已删除 .stale，空间已在文件系统内释放" 'OK'
    return $true
}

function Invoke-Rollback {
    Write-Log "=== 开始回滚 ===" 'WARN'

    if ($script:Renamed) {
        $mv = Invoke-Wsl "mv '$($script:StalePath)' '$($script:SnapshotterPath)' && echo RESTORED"
        if ($mv.ExitCode -eq 0 -and ($mv.Output -join '') -match 'RESTORED') {
            Write-Log "目录名已还原" 'OK'
        } else {
            Write-Log "还原失败！请手工执行：" 'ERR'
            Write-Log "  wsl -d $($script:DistroName) -u root -- mv '$($script:StalePath)' '$($script:SnapshotterPath)'" 'ERR'
        }
    }

    if (-not (Test-DockerDaemon)) { docker desktop start 2>&1 | Out-Null }
    if (Wait-DockerDaemon -TimeoutSec 120) {
        Start-Stack | Out-Null
        $state = Get-StackState
        Write-Log ("回滚后状态：运行 {0}/{1} 异常 {2}" -f $state.Running, $state.Total, $state.Unhealthy) 
    } else {
        Write-Log "Docker 未能启动，请手动启动 Docker Desktop 后检查" 'ERR'
    }
    Write-Log "=== 回滚结束 ===" 'WARN'
}

# ==============================================================================
# Main
# ==============================================================================

function Main {
    Write-Host ""
    Write-Host "${Bold}========================================${Reset}"
    Write-Host "${Bold}  containerd 遗留快照回收（可逆）${Reset}"
    Write-Host "${Bold}  时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')${Reset}"
    Write-Host "${Bold}========================================${Reset}"
    Write-Host ""

    Write-Log "日志: $script:LogFile"

    # 闸口 1 必须在任何写操作之前：备份不可恢复就直接退出
    if (-not (Test-BackupGate)) { exit 3 }

    Write-Step "2/7" "环境检查与基线采集..."

    if (-not (Test-Path -LiteralPath $script:ComposeFile)) {
        Write-Log "找不到 compose 文件：$($script:ComposeFile)" 'ERR'
        exit 1
    }

    if (-not (Test-DockerDaemon)) {
        Write-Log "Docker 未运行，先启动以采集基线..."
        docker desktop start 2>&1 | Out-Null
        if (-not (Wait-DockerDaemon -TimeoutSec $script:DaemonTimeoutSec)) {
            Write-Log "Docker 无法启动，中止" 'ERR'
            exit 1
        }
    }

    $exists = Invoke-Wsl "test -d '$($script:SnapshotterPath)' && echo YES || echo NO"
    if (($exists.Output -join '') -notmatch 'YES') {
        Write-Log "找不到 $($script:SnapshotterPath)（已清理过，或 Docker Desktop 布局不同）" 'ERR'
        exit 1
    }

    $script:BaselineImages = Get-ImageCount
    $baselineState = Get-StackState
    $script:BaselineRunning = $baselineState.Running
    $vhdxBefore = Get-VhdxSizeGB

    Write-Log ("基线：镜像 {0} 个，运行容器 {1} 个，vhdx {2}GB" -f $script:BaselineImages, $script:BaselineRunning, $vhdxBefore)

    if ($script:BaselineRunning -eq 0) {
        Write-Log "当前没有运行中的容器。建议先拉起生产栈再执行，否则无法验证清理后的可用性" 'WARN'
        if (-not $Force) {
            $resp = Read-Host "${Bold}继续? [y/N]:${Reset}"
            if ($resp -notmatch '^[Yy]$') { Write-Log "已取消" 'WARN'; exit 0 }
        }
    }

    Confirm-Proceed

    if (-not (Stop-Containers)) { Write-Log "停止容器阶段失败，未做任何改动" 'ERR'; exit 5 }
    if (-not (Rename-Snapshotter)) { Write-Log "改名失败，未做任何改动" 'ERR'; exit 6 }

    if (-not (Restart-Docker)) {
        Write-Log "重启 Docker 失败，触发回滚" 'ERR'
        Invoke-Rollback
        exit 7
    }

    if (-not (Test-AfterCleanup)) {
        Write-Log "验证未通过，触发回滚" 'ERR'
        Invoke-Rollback
        exit 7
    }

    if (-not (Remove-Stale)) { exit 8 }

    $vhdxAfter = Get-VhdxSizeGB
    Write-Host ""
    Write-Host "${Bold}========================================${Reset}"
    Write-Host "  vhdx : ${vhdxBefore}GB → ${vhdxAfter}GB（删除后尚未压缩）"
    Write-Host "${Bold}========================================${Reset}"
    Write-Host ""
    Write-Log "文件系统内已释放空间，但 vhdx 需要压缩才会真正变小" 'WARN'
    Write-Log "下一步：pwsh -File $($script:ScriptDir)\docker-compact.ps1 -Restart -StartStack"

    if ($Compact) {
        $compactScript = Join-Path $script:ScriptDir 'docker-compact.ps1'
        if (Test-Path -LiteralPath $compactScript) {
            Write-Log "调用 docker-compact.ps1 压缩 vhdx..."
            & $compactScript -Restart -StartStack
            Write-Log "压缩脚本退出码：$LASTEXITCODE"
        } else {
            Write-Log "未找到 docker-compact.ps1，跳过压缩" 'WARN'
        }
    }

    Write-Log "完成" 'OK'
    exit 0
}

# 兜底：任何未捕获异常都尝试回滚，避免留下半成品状态
try {
    Main
} catch {
    Write-Log "未捕获异常：$($_.Exception.Message)" 'ERR'
    if ($script:Renamed) { Invoke-Rollback }
    exit 1
}
