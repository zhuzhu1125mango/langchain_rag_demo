#Requires -Version 7.0
<#
.SYNOPSIS
    Docker Desktop WSL2 虚拟磁盘回收（fstrim → 停 Docker → Optimize-VHD）

.DESCRIPTION
    清理 Docker 内部数据（docker system prune / volume prune）只是把 ext4 里的块
    标记为空闲，docker_data.vhdx 在 Windows 眼里并不会变小。WSL2 的 vhdx 只增不减，
    要真正把空间还给 C 盘，必须三步走：

      1. fstrim        让 ext4 主动告知底层「这些块已废弃」。
                       Optimize-VHD 只认全零块，不做 trim 的话被删数据留下的旧字节
                       会被当成有效数据，压缩工具直接跳过。
                       （实测 2026-11-11：Docker Desktop 关着时 wsl -d docker-desktop 仍能进，
                       但只有 90MB 的根盘存在，trim 报 48MiB 就结束——74GB 的
                       docker_data.vhdx 没挂载，一个块都没碰，压缩必然零收益。
                       所以顺序不能反：先起 Docker（数据盘挂载）→ trim → 停 Docker → 压缩。）
      2. 停 Docker     vhdx 被 WSL 占用时无法压缩，必须先停机。
      3. Optimize-VHD  压缩 vhdx，回收空闲块。

    注意：不要用 `wsl --manage <distro> --set-sparse true --allow-unsafe` 走稀疏文件路线。
    微软因 vhdx 数据损坏案例默认禁用了稀疏 VHD，这块盘上跑着 postgres/milvus 的生产数据，
    不值得为省空间赌数据完整性。定期跑本脚本即可。

.PARAMETER Restart
    压缩完成后重新启动 Docker Desktop

.PARAMETER StartStack
    压缩完成后拉起生产栈（隐含 -Restart，调用 start-prod.ps1）

.PARAMETER Force
    跳过「生产容器正在运行」的交互确认。计划任务/无人值守必须带此参数

.PARAMETER SkipFstrim
    跳过 fstrim 步骤（压缩效果会明显变差，仅在 fstrim 报错时用于排查）

.PARAMETER VhdxPath
    覆盖默认的 docker_data.vhdx 路径（Docker Desktop 改了磁盘镜像位置时用）

.EXAMPLE
    .\scripts\docker-compact.ps1

.EXAMPLE
    .\scripts\docker-compact.ps1 -Restart -StartStack

.EXAMPLE
    # 计划任务入口（无交互终端）
    pwsh -NoProfile -File .\scripts\docker-compact.ps1 -Force -Restart

.NOTES
    用法：./scripts/docker-compact.ps1 [-Restart] [-StartStack] [-Force] [-SkipFstrim]
    必须有管理员权限（Optimize-VHD / diskpart 都要求）。
    退出码：0 成功 / 1 参数或环境错误 / 2 非管理员 / 3 vhdx 不存在
            4 确认未通过 / 5 压缩失败
#>

param(
    [switch]$Restart,
    [switch]$StartStack,
    [switch]$Force,
    [switch]$SkipFstrim,
    [string]$VhdxPath
)

$ErrorActionPreference = 'Stop'

# ==============================================================================
# Configuration
# ==============================================================================

$script:ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent
$script:RootDir = Split-Path $script:ScriptDir -Parent
$script:LogDir = Join-Path $script:RootDir 'backups/compact'
$script:LogFile = Join-Path $script:LogDir ("docker-compact_{0}.log" -f (Get-Date -Format 'yyyyMMdd'))

$script:VhdxPath = if ($VhdxPath) { $VhdxPath } else { Join-Path $env:LOCALAPPDATA 'Docker\wsl\disk\docker_data.vhdx' }
$script:DistroName = 'docker-desktop'
# nsenter 边车镜像：本地已有、带 busybox sh + nsenter 即可（避免联网拉镜像）
$script:SidecarImage = 'redis:7.2-alpine'
$script:DaemonTimeoutSec = 180

if ($StartStack) { $Restart = $true }

New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null

# 计划任务里没有交互终端：不设 UTF-8 会把中文打成乱码，日志就没法看
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

# 控制台 + 落盘双写：计划任务跑的时候没人看屏幕，事后只能靠日志
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

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-VhdxSizeGB {
    if (-not (Test-Path -LiteralPath $script:VhdxPath)) { return 0 }
    return [math]::Round((Get-Item -LiteralPath $script:VhdxPath).Length / 1GB, 2)
}

function Get-FreeSpaceGB {
    $drive = (Get-PSDrive -Name ((Split-Path -Qualifier $script:VhdxPath).TrimEnd(':'))).Free
    return [math]::Round($drive / 1GB, 1)
}

# 在 VM 内执行命令。新版 Docker Desktop 把引擎跑在 distro 内部的系统容器里，
# docker_data.vhdx 只挂在那一层的 mount namespace —— `wsl -d docker-desktop` 进去
# 根本看不到 /mnt/docker-desktop-disk（2026-11-11 09:40 实测：df 只剩 128MB 根盘，
# 数据盘不在里面，导致 fstrim 只 trim 到 /）。用特权容器 nsenter 到 VM init(pid 1)
# 的 namespace 才能访问，且不依赖被安全策略拦截的 wsl.exe。
function Invoke-InVm {
    param([string]$Command)
    # 空镜像名会让 docker run 把 -c 当成镜像名，静默失败后又被上层误判成"数据盘未挂载"。
    # 2026-11-11 踩过：配置里漏了 SidecarImage，排查绕了一圈。这里直接报清楚。
    if ([string]::IsNullOrWhiteSpace($script:SidecarImage)) {
        throw "SidecarImage 未配置，无法用 nsenter 访问 VM"
    }
    $out = & docker run --rm --privileged --pid=host --entrypoint sh $script:SidecarImage -c "nsenter -t 1 -m -- $Command" 2>&1
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = @($out) }
}

# 数据盘真实占用。压缩能回收多少只取决于「vhdx 文件大小 − 盘内已用」这个差值，
# 与 fstrim 报的 trimmed 字节数无关（实测 2026-11-11：trim 报 934GB，实际只回收 0.09GB，
# 因为 ext4 的空闲块从未分配进 vhdx，trim 它们对文件大小毫无影响）。
function Get-DataDiskUsageGB {
    $vm = Invoke-InVm 'df -Pk'
    if ($vm.ExitCode -ne 0) { return $null }

    foreach ($line in $vm.Output) {
        if ($line -notmatch 'docker-desktop-disk') { continue }
        $fields = @($line -split '\s+' | Where-Object { $_ })
        # Filesystem 1024-blocks Used Available Capacity Mounted-on
        if ($fields.Count -ge 6) {
            $usedKB = 0.0
            if ([double]::TryParse($fields[2], [ref]$usedKB)) {
                return [pscustomobject]@{
                    Mount = $fields[5]
                    UsedGB = [math]::Round($usedKB / 1048576, 2)
                }
            }
        }
    }
    return $null
}

function Test-DockerDaemon {    # docker info 失败说明守护进程没起（Docker Desktop 停着时 npipe 不存在）
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

function Wait-VhdxUnlocked {
    param([int]$TimeoutSec = 60)
    $elapsed = 0
    while ($elapsed -lt $TimeoutSec) {
        try {
            # 独占打开能成功 = 没有进程持有句柄，可以安全压缩
            $fs = [System.IO.File]::Open($script:VhdxPath, 'Open', 'ReadWrite', 'None')
            $fs.Close()
            return $true
        } catch {
            Start-Sleep -Milliseconds 800
            $elapsed += 1
        }
    }
    return $false
}

function Get-RunningContainerCount {
    if (-not (Test-DockerDaemon)) { return 0 }
    $names = docker ps --format '{{.Names}}' 2>$null
    if (-not $names) { return 0 }
    return @($names).Count
}

# ==============================================================================
# Steps
# ==============================================================================

function Ensure-DockerRunning {
    Write-Step "2/6" "确保 Docker Desktop 运行（数据盘必须挂载才能 trim）..."

    if (Test-DockerDaemon) {
        Write-Log "Docker 已在运行" 'OK'
    } else {
        Write-Log "Docker 未运行，尝试启动..."
        docker desktop start 2>&1 | Out-Null

        if (-not (Wait-DockerDaemon -TimeoutSec $script:DaemonTimeoutSec)) {
            Write-Log "Docker 未能在 $($script:DaemonTimeoutSec) 秒内就绪" 'ERR'
            Write-Log "请手动启动 Docker Desktop（等托盘变绿）后重跑本脚本" 'ERR'
            return $false
        }
        # 守护进程就绪后 WSL 里的 docker_data 挂载还需要一点时间
        Start-Sleep -Seconds 5
        Write-Log "Docker 已启动" 'OK'
    }

    # 守护进程跑起来 ≠ 数据盘挂载成功。必须实测，不能靠推断
    # （2026-11-11 踩过：只验证了 daemon 就打印"数据盘已挂载"，结果 fstrim 只 trim 到 /）
    $df = Invoke-InVm "df -h /mnt/docker-desktop-disk 2>/dev/null | tail -1"
    if ($df.ExitCode -ne 0 -or -not ($df.Output -match 'docker-desktop-disk')) {
        Write-Log "数据盘 /mnt/docker-desktop-disk 未挂载（df 无此挂载点）" 'ERR'
        Write-Log "请确认 Docker Desktop 完全启动（托盘绿灯）后重跑" 'ERR'
        return $false
    }
    Write-Log "数据盘已挂载：$((($df.Output -join ' ') -replace '\s+', ' ').Trim())" 'OK'
    return $true
}

function Invoke-Fstrim {
    Write-Step "3/6" "向数据盘发送 TRIM（标记空闲块）..."

    # 只读操作：fstrim 只标记空闲块，不动任何有效数据
    $vm = Invoke-InVm 'fstrim -av'
    $output = $vm.Output
    $code = $vm.ExitCode

    if ($code -ne 0) {
        Write-Log "fstrim 失败（exit=$code）：$($output -join ' ')" 'ERR'
        Write-Log "常见原因：发行版名不是 $($script:DistroName)，或 WSL 版本过旧" 'ERR'
        return [pscustomobject]@{ Ok = $false; TrimmedGB = 0; DataMountFound = $false }
    }

    # fstrim -av 每个挂载点一行："<mount>: x MiB (n bytes) trimmed"
    # 单位兼容 GB/GiB/MB/MiB/TiB —— util-linux 不同版本输出不一致，只认 GiB 会漏行
    $mounts = @()
    foreach ($line in @($output)) {
        if (-not $line) { continue }
        if ($line -match '^(.+?):\s+[\d.]+\s*(?:TiB|GiB|MiB|KiB|TB|GB|MB|KB)\s*\((\d+)\s*bytes\)\s*trimmed') {
            $mounts += [pscustomobject]@{
                Mount = $matches[1].Trim()
                Bytes = [double]$matches[2]
            }
        } else {
            Write-Log "  $line"
        }
    }

    $totalGB = 0.0
    foreach ($m in $mounts) {
        $gb = [math]::Round($m.Bytes / 1GB, 2)
        $totalGB += $gb
        Write-Log ("  {0}: {1}GB trimmed" -f $m.Mount, $gb)
    }
    Write-Log "fstrim 完成，合计可回收 $([math]::Round($totalGB, 2))GB" 'OK'

    # 关键判据：只有 "/" 被 trim，说明 docker_data.vhdx 没挂载。
    # 2026-11-11 实测踩过：Docker Desktop 关着时 wsl -d docker-desktop 仍能进，
    # 但只有 90MB 的根盘（/dev/sdd）存在，trim 出 48MiB 就结束，
    # 74GB 的数据盘一个块都没碰 → 后续压缩必然零收益。
    $dataMounts = @($mounts | Where-Object { $_.Mount -ne '/' })
    if ($dataMounts.Count -eq 0) {
        Write-Log "只 trim 到 /，docker_data.vhdx 未挂载：Docker Desktop 没在运行" 'ERR'
        Write-Log "此时 trim 与压缩对数据盘都无效。请确认 Docker Desktop 启动完成（托盘绿灯）后重跑" 'ERR'
        return [pscustomobject]@{ Ok = $false; TrimmedGB = 0; DataMountFound = $false }
    }

    if ($totalGB -lt 1) {
        # 不再据此判断"没有收益"：WSL2 的 discard 不穿透到 vhdx，trimmed 常年接近 0
        Write-Log "trimmed 仅 $([math]::Round($totalGB, 2))GB（WSL2 的 discard 不穿透 vhdx，属正常）" 'WARN'
        Write-Log "是否有收益看下面按 df 实占算出的余量，不以 trimmed 为准" 'WARN'
    }
    return [pscustomobject]@{ Ok = $true; TrimmedGB = $totalGB; DataMountFound = $true }
}

function Stop-DockerDesktop {
    Write-Step "4/6" "停止 Docker Desktop 与 WSL..."

    if (Test-DockerDaemon) {
        # 优先官方 CLI，会做优雅关闭（通知容器、刷盘、正常卸载 ext4）
        docker desktop stop 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Log "docker desktop stop 返回非零，改按句柄状态决定是否硬停" 'WARN'
        }
    } else {
        Write-Log "Docker 已在停止状态" 'OK'
    }

    # 先给优雅关闭留时间；句柄没释放才动用 wsl --shutdown。
    # 硬停正在写盘的 ext4 是孤儿 inode 的典型成因——2026-11-11 疑似踩过：
    # 数据盘 df 报 70.4GB 已用、du 只见 19.3GB、deleted 句柄为 0，
    # 51GB 成了无文件可达的僵尸块，e2fsck 在 WSL 下也修不了。
    # 结论：能优雅关就绝不硬停。
    if (-not (Wait-VhdxUnlocked -TimeoutSec 45)) {
        Write-Log "优雅关闭 45 秒后 vhdx 仍被占用，执行 wsl --shutdown（有孤儿 inode 风险）" 'WARN'
        wsl.exe --shutdown 2>&1 | Out-Null
        Start-Sleep -Seconds 2
    }

    if (-not (Wait-VhdxUnlocked -TimeoutSec 30)) {
        Write-Log "vhdx 仍被占用，无法压缩" 'ERR'
        Write-Log "请确认 Docker Desktop 已完全退出（托盘右键 Quit），再重跑本脚本" 'ERR'
        return $false
    }

    Write-Log "vhdx 句柄已释放，可以压缩" 'OK'
    return $true
}

function Invoke-Compact {
    Write-Step "5/6" "压缩 vhdx（Mode=Full，通常需要几分钟）..."

    $optimize = Get-Command Optimize-VHD -ErrorAction SilentlyContinue
    if ($optimize) {
        try {
            Optimize-VHD -Path $script:VhdxPath -Mode Full -ErrorAction Stop
            Write-Log "Optimize-VHD 完成" 'OK'
            return $true
        } catch {
            Write-Log "Optimize-VHD 失败：$($_.Exception.Message)" 'WARN'
            Write-Log "回退到 diskpart compact vdisk" 'WARN'
        }
    } else {
        Write-Log "未找到 Optimize-VHD（需 Hyper-V 模块），回退到 diskpart" 'WARN'
    }

    # 回退路径：diskpart 的 compact 只认全零块，效果不如 Optimize-VHD，但无需 Hyper-V
    $scriptFile = Join-Path $env:TEMP 'docker-compact-diskpart.txt'
    try {
        @(
            "select vdisk file=`"$script:VhdxPath`""
            "compact vdisk"
        ) | Set-Content -LiteralPath $scriptFile -Encoding ascii

        $output = diskpart /s $scriptFile 2>&1
        $code = $LASTEXITCODE
        foreach ($line in @($output)) { if ($line) { Write-Log "  $line" } }

        if ($code -ne 0) {
            Write-Log "diskpart 压缩失败（exit=$code）" 'ERR'
            return $false
        }
        Write-Log "diskpart 压缩完成" 'OK'
        return $true
    } finally {
        Remove-Item -LiteralPath $scriptFile -Force -ErrorAction SilentlyContinue
    }
}

function Start-DockerDesktop {
    Write-Step "6/6" "重新启动 Docker Desktop..."

    docker desktop start 2>&1 | Out-Null
    if (Wait-DockerDaemon -TimeoutSec $script:DaemonTimeoutSec) {
        Write-Log "Docker 守护进程已就绪" 'OK'
        return $true
    }

    Write-Log "Docker 守护进程在 $($script:DaemonTimeoutSec) 秒内未就绪，请手动启动 Docker Desktop" 'ERR'
    return $false
}

# ==============================================================================
# Main
# ==============================================================================

function Print-Banner {
    Write-Host ""
    Write-Host "${Bold}========================================${Reset}"
    Write-Host "${Bold}  Docker 虚拟磁盘回收${Reset}"
    Write-Host "${Bold}  时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')${Reset}"
    Write-Host "${Bold}========================================${Reset}"
    Write-Host ""
}

function Main {
    Print-Banner

    Write-Step "1/6" "环境检查..."

    if (-not (Test-Admin)) {
        Write-Log "需要管理员权限（Optimize-VHD / diskpart 都要求）" 'ERR'
        Write-Log "请以管理员身份重开终端，或：Start-Process pwsh -Verb RunAs" 'ERR'
        exit 2
    }
    Write-Log "管理员权限 OK" 'OK'

    if (-not (Test-Path -LiteralPath $script:VhdxPath)) {
        Write-Log "找不到 vhdx：$script:VhdxPath" 'ERR'
        Write-Log "请在 Docker Desktop > Settings > Resources > Advanced 确认磁盘镜像位置，或用 -VhdxPath 指定" 'ERR'
        exit 3
    }

    $beforeGB = Get-VhdxSizeGB
    $beforeFreeGB = Get-FreeSpaceGB
    Write-Log "当前 vhdx: ${beforeGB}GB   C 盘可用: ${beforeFreeGB}GB"
    Write-Log "日志: $script:LogFile"

    # 压缩必须停机，等于把生产栈短暂下线——不能默默干
    $running = Get-RunningContainerCount
    if ($running -gt 0) {
        Write-Log "检测到 $running 个运行中的容器，压缩会先停掉它们" 'WARN'

        if (-not $Force) {
            $interactive = -not [Console]::IsInputRedirected
            if (-not $interactive) {
                Write-Log "无交互终端且未指定 -Force，中止（计划任务请加 -Force）" 'ERR'
                exit 4
            }
            Write-Host ""
            $response = Read-Host "${Bold}确认停止全部容器并压缩? [y/N]:${Reset} "
            if ($response -notmatch '^[Yy]$') {
                Write-Log "已取消" 'WARN'
                exit 4
            }
        }
    }

    if (-not $SkipFstrim) {
        # 必须先起 Docker，否则数据盘不挂载，trim 只作用于 90MB 的根盘（实测坑）
        if (-not (Ensure-DockerRunning)) { exit 5 }

        $trim = Invoke-Fstrim
        if (-not $trim.Ok) { exit 5 }

        # 真正决定收益的是「vhdx 文件大小 − 盘内已用」，不是 trimmed 字节数。
        # 2026-11-11 实测：trim 报 934GB、压缩只回收 0.09GB——ext4 的空闲块从未
        # 分配进 vhdx，trim 它们对文件大小毫无影响。当时数据盘实占 70.4GB，
        # vhdx 74.41GB，余量 4GB 全是文件系统元数据开销，压不动。
        $diskUsage = Get-DataDiskUsageGB
        if ($diskUsage) {
            $slackGB = [math]::Round($beforeGB - $diskUsage.UsedGB, 2)
            Write-Log ("数据盘 {0} 实占 {1}GB，vhdx {2}GB，可压缩余量约 {3}GB" -f $diskUsage.Mount, $diskUsage.UsedGB, $beforeGB, $slackGB)
            if ($slackGB -lt 1 -and -not $Force) {
                Write-Log "余量不足 1GB：压缩挤不出东西，跳过且不停止 Docker" 'WARN'
                Write-Log "若 vhdx 确实偏大，问题在盘内数据本身而非碎片，查：wsl -d $($script:DistroName) -u root -- sh -c `"du -sm /mnt/docker-desktop-disk/data/docker/* | sort -n`"" 'WARN'
                exit 0
            }
        } else {
            Write-Log "未能读取数据盘占用，跳过余量预判，继续执行" 'WARN'
        }
    } else {
        Write-Log "已跳过 fstrim（-SkipFstrim）；数据盘未 trim 时压缩收益不确定" 'WARN'
    }

    if (-not (Stop-DockerDesktop)) { exit 5 }

    if (-not (Invoke-Compact)) {
        # 压缩失败但 vhdx 已卸载：至少把 Docker 拉回来，别留个停机环境
        Write-Log "压缩失败，尝试恢复 Docker" 'WARN'
        $null = Start-DockerDesktop
        exit 5
    }

    $afterGB = Get-VhdxSizeGB
    $afterFreeGB = Get-FreeSpaceGB
    $reclaimed = [math]::Round($beforeGB - $afterGB, 2)

    Write-Host ""
    Write-Host "${Bold}========================================${Reset}"
    Write-Host "  vhdx  : ${beforeGB}GB → ${afterGB}GB"
    Write-Host "  C 盘   : 可用 ${beforeFreeGB}GB → ${afterFreeGB}GB"
    Write-Host "${Bold}========================================${Reset}"
    Write-Host ""

    Write-Log "回收 ${reclaimed}GB（vhdx ${beforeGB}GB → ${afterGB}GB）" 'OK'

    if ($reclaimed -le 0) {
        Write-Log "没有可回收空间。若 Docker 内确实已清理过数据，说明这些块未被 fstrim 标记，试试先跑一次不带 -SkipFstrim 的完整流程" 'WARN'
    }

    if ($Restart) {
        if (-not (Start-DockerDesktop)) { exit 5 }

        if ($StartStack) {
            $startScript = Join-Path $script:ScriptDir 'start-prod.ps1'
            if (Test-Path -LiteralPath $startScript) {
                Write-Log "拉起生产栈：$startScript" 
                & $startScript -Force
                if ($LASTEXITCODE -ne 0) {
                    Write-Log "生产栈启动失败，请检查 start-prod.ps1 输出" 'ERR'
                    exit 5
                }
            } else {
                Write-Log "未找到 start-prod.ps1，跳过生产栈启动" 'WARN'
            }
        }
    } else {
        Write-Log "Docker Desktop 保持停止状态。需要时手动启动，或下次直接加 -Restart / -StartStack" 
    }

    Write-Log "完成" 'OK'
    exit 0
}

Main
