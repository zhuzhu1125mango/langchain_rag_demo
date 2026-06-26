#Requires -Version 7.0
<#
.SYNOPSIS
    LangChain RAG Demo - Development Environment Startup Script

.DESCRIPTION
    PowerShell 7 optimized startup script for development environment

.PARAMETER SkipWait
    Skip health check waiting

.EXAMPLE
    .\scripts\start-dev.ps1
    .\scripts\start-dev.ps1 -SkipWait
.NOTES
    用法：./scripts/start-dev.ps1 [-SkipWait]
#>

param(
    [switch]$SkipWait
)

$ErrorActionPreference = 'Stop'

# ==============================================================================
# Configuration
# ==============================================================================

$script:ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent
$script:EnvFileSource = Join-Path $script:ScriptDir "../.env.dev"
$script:EnvFileTarget = Join-Path $script:ScriptDir "../.env"
$script:ComposeFile = Join-Path $script:ScriptDir "../docker-compose.dev.yml"
$script:EnvName = "DEVELOPMENT"
$script:MaxWaitSeconds = $env:MAX_WAIT_SECONDS ? [int]$env:MAX_WAIT_SECONDS : 120
$script:WaitInterval = $env:WAIT_INTERVAL ? [int]$env:WAIT_INTERVAL : 5

# ==============================================================================
# Color Definitions & Logging Functions
# ==============================================================================

function Get-ANSI { param([string]$Code) { "`e[$Code" } }

$Reset = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "0m" } else { "" }
$Red = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "31m" } else { "" }
$Green = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "32m" } else { "" }
$Yellow = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "33m" } else { "" }
$Blue = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "34m" } else { "" }
$Magenta = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "35m" } else { "" }
$Cyan = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "36m" } else { "" }
$Bold = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "1m" } else { "" }

function Write-Info { Write-Host "${Blue}[INFO]${Reset} $args" }
function Write-OK { Write-Host "${Green}[OK]${Reset} $args" }
function Write-Warn { Write-Host "${Yellow}[WARN]${Reset} $args" }
function Write-Err { Write-Host "${Red}[ERROR]${Reset} $args" }
function Write-Step { param([string]$Step, [string]$Message) Write-Host "${Cyan}[$Step]${Reset} $Message" }
function Write-Success { Write-Host "${Green}✓${Reset} $args" }
function Write-Fail { Write-Host "${Red}✗${Reset} $args" }

# ==============================================================================
# Banner
# ==============================================================================

function Print-Banner {
    $currentTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

    Write-Host ""
    Write-Host "${Bold}========================================${Reset}"
    Write-Host "${Bold}  LangChain RAG Demo 启动脚本${Reset}"
    Write-Host "${Bold}  环境: ${Cyan}${script:EnvName}${Reset}"
    Write-Host "${Bold}  时间: ${currentTime}${Reset}"
    Write-Host "${Bold}========================================${Reset}"
    Write-Host ""
}

# ==============================================================================
# Pre-flight Checks
# ==============================================================================

function Test-Command {
    param([string]$Command)
    $null = Get-Command $Command -ErrorAction SilentlyContinue
    return $null -ne $null
}

function Invoke-PreflightChecks {
    Write-Step "1/6" "检测运行环境..."

    # Check docker
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerCmd) {
        $dockerVersion = (docker --version 2>$null | Select-Object -First 1) ?? "unknown"
        Write-OK "Docker 已安装: $dockerVersion"
    } else {
        Write-Err "Docker 未安装，请先安装 Docker"
        exit 1
    }

    # Check docker compose
    $composeVersion = docker compose version 2>$null | Select-Object -First 1
    if ($composeVersion) {
        $script:DockerComposeCmd = "docker compose"
        Write-OK "Docker Compose 已安装: $composeVersion"
    } else {
        $dockerComposeCmd = Get-Command docker-compose -ErrorAction SilentlyContinue
        if ($dockerComposeCmd) {
            $script:DockerComposeCmd = "docker-compose"
            $composeVersion = (docker-compose --version 2>$null | Select-Object -First 1) ?? "unknown"
            Write-OK "Docker Compose 已安装: $composeVersion"
        } else {
            Write-Err "Docker Compose 未安装，请先安装 Docker Compose"
            exit 1
        }
    }

    # Check .env.dev exists
    if (Test-Path $script:EnvFileSource) {
        Write-OK "配置文件 $script:EnvFileSource 存在"
    } else {
        Write-Err "配置文件 $script:EnvFileSource 不存在"
        Write-Info "请创建 .env.dev 文件，可参考以下必要变量:"
        Write-Host "  POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD"
        Write-Host "  MINIO_ROOT_USER, MINIO_ROOT_PASSWORD"
        Write-Host "  MINIO_ACCESS_KEY, MINIO_SECRET_KEY"
        Write-Host "  GF_SECURITY_ADMIN_PASSWORD"
        exit 1
    }

    # Check docker daemon is running
    $null = docker info 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-OK "Docker 守护进程运行中"
    } else {
        Write-Err "Docker 守护进程未运行，请启动 Docker Desktop 或 Docker 服务"
        exit 1
    }

    Write-Host ""
}

# ==============================================================================
# Copy Environment File
# ==============================================================================

function Copy-EnvFile {
    Write-Step "2/6" "复制配置文件..."

    # Backup existing .env if exists
    if (Test-Path $script:EnvFileTarget) {
        $timestamp = Get-Date -Format "yyyyMMddHHmmss"
        $backupFile = "$($script:EnvFileTarget).backup.$timestamp"
        Copy-Item $script:EnvFileTarget $backupFile -Force
        Write-Info "已备份旧配置到 $backupFile"
    }

    # Copy .env.dev to .env
    try {
        Copy-Item $script:EnvFileSource $script:EnvFileTarget -Force
        Write-OK "已复制 $script:EnvFileSource -> $script:EnvFileTarget"
    } catch {
        Write-Err "复制配置文件失败: $_"
        exit 1
    }

    Write-Host ""
}

# ==============================================================================
# Load Environment Variables
# ==============================================================================

function Load-EnvVars {
    Write-Step "3/6" "加载环境变量..."

    # Load .env file
    Get-Content $script:EnvFileTarget | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            [System.Environment]::SetEnvironmentVariable($name, $value)
        }
    }

    Write-OK "环境变量已加载"
    Write-Host ""
}

# ==============================================================================
# Start Docker Compose Services
# ==============================================================================

function Start-Services {
    Write-Step "4/6" "启动 Docker Compose 服务..."
    Write-Info "拉取/构建镜像中，请耐心等待..."
    Write-Host ""

    & docker compose -f $script:ComposeFile up -d --build
    if ($LASTEXITCODE -ne 0) {
        Write-Err "服务启动失败"
        Write-Info "请检查 Docker 日志: docker compose -f $script:ComposeFile logs"
        exit 1
    }

    Write-Host ""
    Write-OK "服务启动命令执行成功"
    Write-Host ""
}

# ==============================================================================
# Health Check
# ==============================================================================

function Wait-ForHealth {
    Write-Step "5/6" "等待服务健康检查..."

    # Check if SkipWait is set
    if ($SkipWait -or $env:SKIP_WAIT -eq "1") {
        Write-Warn "已跳过健康检查等待 (SkipWait=$true)"
        Write-Host ""
        return $true
    }

    $elapsed = 0
    $allHealthy = $false

    Write-Host ""

    while ($elapsed -lt $script:MaxWaitSeconds) {
        # Get container status
        $statusOutput = docker compose -f $script:ComposeFile ps --format "table {{.Name}}`t{{.State}}`t{{.Status}}" 2>$null

        # Count containers and healthy ones
        $totalContainers = 0
        $healthyContainers = 0

        # Parse status
        $statusOutput | ForEach-Object {
            $parts = $_ -split "`t"
            if ($parts.Count -ge 3) {
                $name = $parts[0].Trim()
                $state = $parts[1].Trim()
                $status = $parts[2].Trim()

                if ([string]::IsNullOrEmpty($name) -or $name -eq "NAME") { return }

                $totalContainers++

                if ($state -eq "running") {
                    if ($status -match "healthy" -or $status -notmatch "health") {
                        $healthyContainers++
                    }
                }
            }
        }

        # Print current status
        $line = "  等待中... ($elapsed/$($script:MaxWaitSeconds) 秒) - 健康: $healthyContainers/$totalContainers"
        Write-Host -NoNewline "`r$line"
        Write-Host -NoNewline (" " * (80 - $line.Length))

        # Check if all are healthy
        if ($totalContainers -gt 0 -and $healthyContainers -eq $totalContainers) {
            $allHealthy = $true
            break
        }

        Start-Sleep -Seconds $script:WaitInterval
        $elapsed += $script:WaitInterval
    }

    Write-Host ""

    if ($allHealthy) {
        Write-OK "所有服务已就绪"
    } else {
        Write-Warn "部分服务未在 $($script:MaxWaitSeconds) 秒内完全就绪"
        Write-Info "请使用以下命令查看详细状态:"
        Write-Host "    docker compose -f $script:ComposeFile ps"
    }

    Write-Host ""

    # Print container status table
    Print-ContainerStatus

    return $allHealthy
}

function Print-ContainerStatus {
    Write-Info "容器状态:"
    Write-Host ""
    docker compose -f $script:ComposeFile ps --format "table {{.Name}}`t{{.State}}`t{{.Status}}" 2>$null
    Write-Host ""
}

# ==============================================================================
# Print Access Information
# ==============================================================================

function Print-AccessInfo {
    Write-Step "6/6" "输出访问信息..."
    Write-Host ""

    # Read env vars for display (dev mode shows actual values)
    $pgDb = $env:POSTGRES_DB ?? "rag_demo"
    $pgUser = $env:POSTGRES_USER ?? "postgres"
    $pgPwd = $env:POSTGRES_PASSWORD ?? "postgres"
    $minioUser = $env:MINIO_ROOT_USER ?? "minioadmin"
    $minioPwd = $env:MINIO_ROOT_PASSWORD ?? "minioadmin"
    $grafanaPwd = $env:GF_SECURITY_ADMIN_PASSWORD ?? "admin"

    Write-Host "${Bold}========================================${Reset}"
    Write-Host "${Bold}  访问地址汇总${Reset}"
    Write-Host "${Bold}========================================${Reset}"
    Write-Host ""
    Write-Host "  ${Cyan}前端 & API${Reset}"
    Write-Host "    前端页面     : http://localhost:5173"
    Write-Host "    Backend API  : http://localhost:8000"
    Write-Host "    API 文档     : http://localhost:8000/docs"
    Write-Host ""
    Write-Host "  ${Cyan}存储服务${Reset}"
    Write-Host "    PostgreSQL   : localhost:5433"
    Write-Host "                  数据库: $pgDb"
    Write-Host "                  用户名: $pgUser"
    Write-Host "                  密码  : $pgPwd"
    Write-Host "    MinIO 控制台 : http://localhost:9001"
    Write-Host "                  用户名: $minioUser"
    Write-Host "                  密码  : $minioPwd"
    Write-Host "    Milvus       : localhost:19530"
    Write-Host ""
    Write-Host "  ${Cyan}监控服务${Reset}"
    Write-Host "    Prometheus   : http://localhost:9090"
    Write-Host "    Grafana      : http://localhost:3000"
    Write-Host "                  用户名: admin"
    Write-Host "                  密码  : $grafanaPwd"
    Write-Host ""
    Write-Host "${Bold}========================================${Reset}"
    Write-Host "  ${Yellow}常用命令${Reset}"
    Write-Host "${Bold}========================================${Reset}"
    Write-Host "    查看日志  : .\scripts\logs-dev.ps1"
    Write-Host "    停止服务  : .\scripts\stop-dev.ps1"
    Write-Host "    查看状态  : docker compose -f $script:ComposeFile ps"
    Write-Host "    进入容器  : docker exec -it <container_name> powershell"
    Write-Host ""
    Write-Host "${Bold}========================================${Reset}"
    Write-Host ""
}

# ==============================================================================
# Main
# ==============================================================================

function Main {
    Print-Banner
    Invoke-PreflightChecks
    Copy-EnvFile
    Load-EnvVars
    Start-Services
    $healthResult = Wait-ForHealth
    Print-AccessInfo

    if ($healthResult) {
        Write-Host "${Green}${Bold}✓ 开发环境启动完成！${Reset}"
    } else {
        Write-Host "${Yellow}${Bold}! 开发环境已启动，但部分服务未完全就绪${Reset}"
        Write-Host "${Yellow}${Bold}! 请检查容器状态并确保所有服务正常运行${Reset}"
        exit 1
    }
    Write-Host ""
}

Main
