#Requires -Version 7.0
<#
.SYNOPSIS
    LangChain RAG Demo - Production Environment Startup Script

.DESCRIPTION
    PowerShell 7 optimized startup script for production environment
    Includes security checks and confirmation prompts

.PARAMETER SkipWait
    Skip health check waiting

.PARAMETER Force
    Skip confirmation prompt

.EXAMPLE
    .\scripts\start-prod.ps1
    .\scripts\start-prod.ps1 -SkipWait -Force
.NOTES
    用法：./scripts/start-prod.ps1 [-SkipWait] [-Force]
#>

param(
    [switch]$SkipWait,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# ==============================================================================
# Configuration
# ==============================================================================

$script:ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent
$script:EnvFile = Join-Path $script:ScriptDir "../.env.prod"
$script:ComposeFile = Join-Path $script:ScriptDir "../docker-compose.yml"
$script:ComposeArgs = @("compose", "-f", $script:ComposeFile, "--env-file", $script:EnvFile)
$script:EnvName = "PRODUCTION"
$script:MaxWaitSeconds = $env:MAX_WAIT_SECONDS ? [int]$env:MAX_WAIT_SECONDS : 180
$script:WaitInterval = $env:WAIT_INTERVAL ? [int]$env:WAIT_INTERVAL : 5

# Required variables for production
$script:RequiredVars = @(
    "POSTGRES_PASSWORD"
    "MINIO_ROOT_PASSWORD"
    "MINIO_SECRET_KEY"
    "GF_SECURITY_ADMIN_PASSWORD"
    "SECRET_KEY"
)

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
    Write-Host "${Bold}  环境: ${Red}${script:EnvName}${Reset}"
    Write-Host "${Bold}  时间: ${currentTime}${Reset}"
    Write-Host "${Bold}========================================${Reset}"
    Write-Host ""
    Write-Host "${Yellow}⚠ 警告: 这是生产环境启动脚本${Reset}"
    Write-Host "${Yellow}⚠ 请确保已正确配置所有安全相关参数${Reset}"
    Write-Host ""
}

# ==============================================================================
# Confirmation Prompt
# ==============================================================================

function Confirm-Start {
    # Skip if Force is set
    if ($Force -or $env:FORCE_START -eq "1") {
        Write-Info "已跳过确认提示 (Force=$true)"
        return
    }

    Write-Host "${Yellow}请确认以下事项:${Reset}"
    Write-Host "  1. .env.prod 文件已正确配置"
    Write-Host "  2. 所有密码已修改为强密码"
    Write-Host "  3. 数据备份已完成"
    Write-Host ""

    $response = Read-Host "${Bold}是否继续? [y/N]:${Reset} "
    Write-Host ""

    if ($response -notmatch "^[Yy]$") {
        Write-Info "已取消启动"
        exit 0
    }
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
    Write-Step "1/7" "检测运行环境..."

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

    # Check .env.prod exists
    if (Test-Path $script:EnvFile) {
        Write-OK "配置文件 $script:EnvFile 存在"
    } else {
        Write-Err "配置文件 $script:EnvFile 不存在"
        Write-Info "请创建 .env.prod 文件，必须包含以下安全变量:"
        foreach ($var in $script:RequiredVars) {
            Write-Host "  - $var"
        }
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
# Security Checks
# ==============================================================================

function Invoke-SecurityChecks {
    Write-Step "2/7" "执行安全检查..."

    # Temporarily load env file to check variables
    Get-Content $script:EnvFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            Set-Item -Path "env:$name" -Value $value -ErrorAction SilentlyContinue
        }
    }

    $hasIssues = $false

    foreach ($var in $script:RequiredVars) {
        $value = [System.Environment]::GetEnvironmentVariable($var)

        if ([string]::IsNullOrEmpty($value)) {
            Write-Err "变量 $var 未设置或为空"
            $hasIssues = $true
        } elseif ($value -eq "admin" -or $value -eq "password" -or $value -eq "changeme") {
            Write-Warn "变量 $var 使用了不安全的默认值"
            $hasIssues = $true
        } else {
            Write-OK "变量 $var 已正确设置"
        }
    }

    if ($hasIssues) {
        Write-Host ""
        Write-Err "安全检查未通过，请修正上述问题后重试"
        exit 1
    }

    Write-Host ""
}

# ==============================================================================
# Load Environment Variables
# ==============================================================================

function Load-EnvVars {
    Write-Step "3/7" "加载环境变量..."

    # Load .env.prod file（用于脚本内展示；compose 经 --env-file 自行读取同一文件）
    Get-Content $script:EnvFile | ForEach-Object {
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
    Write-Step "4/7" "启动 Docker Compose 服务..."
    Write-Info "拉取/构建镜像中，请耐心等待..."
    Write-Host ""

    & docker @script:ComposeArgs up -d --build
    if ($LASTEXITCODE -ne 0) {
        Write-Err "服务启动失败"
        Write-Info "请检查 Docker 日志: docker $script:ComposeArgs logs"
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
    Write-Step "5/7" "等待服务健康检查..."

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
        $statusOutput = docker @script:ComposeArgs ps --format "table {{.Name}}`t{{.State}}`t{{.Status}}" 2>$null

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
        Write-Host "    docker $script:ComposeArgs ps"
    }

    Write-Host ""

    # Print container status table
    Print-ContainerStatus

    return $allHealthy
}

# ==============================================================================
# Database Migration (Alembic)
# ==============================================================================

function Invoke-DatabaseMigration {
    Write-Step "6/7" "执行数据库迁移（alembic upgrade head）..."

    # 容器内 POSTGRES_HOST=postgres 直连数据库（alembic 为主依赖，prod 镜像可用）
    & docker @script:ComposeArgs exec -T backend alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        # 全新数据库：backend 启动时 init_db(create_all) 已建全量表但无 alembic_version，
        # upgrade 撞 DuplicateTableError；此时 schema 与当前镜像模型一致，stamp 对齐即可
        Write-Warn "upgrade 未执行，尝试按全新库对齐 alembic 版本（stamp head）..."
        & docker @script:ComposeArgs exec -T backend alembic stamp head
        if ($LASTEXITCODE -ne 0) {
            Write-Err "数据库迁移失败，请检查 backend 容器日志"
            Write-Info "查看日志: docker $script:ComposeArgs logs backend"
            exit 1
        }
        Write-OK "已对齐 alembic 版本（全新库由启动建表）"
    } else {
        Write-OK "数据库迁移完成"
    }
    Write-Host ""
}

function Print-ContainerStatus {
    Write-Info "容器状态:"
    Write-Host ""
    docker @script:ComposeArgs ps --format "table {{.Name}}`t{{.State}}`t{{.Status}}" 2>$null
    Write-Host ""
}

# ==============================================================================
# Print Access Information
# ==============================================================================

function Print-AccessInfo {
    Write-Step "7/7" "输出访问信息..."
    Write-Host ""

    # In production, only show variable names, not actual values
    $pgDb = $env:POSTGRES_DB ?? "rag_demo"
    $pgUser = $env:POSTGRES_USER ?? "postgres"

    Write-Host "${Bold}========================================${Reset}"
    Write-Host "${Bold}  访问地址汇总${Reset}"
    Write-Host "${Bold}========================================${Reset}"
    Write-Host ""
    Write-Host "  ${Cyan}前端 & API${Reset}"
    Write-Host "    前端页面     : http://localhost:80"
    Write-Host "    Backend API  : http://localhost:8001（仅回环绑定）"
    Write-Host "    API 文档     : http://localhost:8001/docs"
    Write-Host ""
    Write-Host "  ${Cyan}存储服务${Reset}"
    Write-Host "    PostgreSQL   : localhost:5434"
    Write-Host "                  数据库: $pgDb"
    Write-Host "                  用户名: $pgUser"
    Write-Host "                  密码  : `${POSTGRES_PASSWORD}"
    Write-Host "    MinIO 控制台 : http://localhost:9003"
    Write-Host "                  用户名: `${MINIO_ROOT_USER}"
    Write-Host "                  密码  : `${MINIO_ROOT_PASSWORD}"
    Write-Host "    Milvus       : localhost:19531"
    Write-Host ""
    Write-Host "  ${Cyan}监控服务${Reset}"
    Write-Host "    Prometheus   : http://localhost:9094"
    Write-Host "    Grafana      : http://localhost:3001"
    Write-Host "                  用户名: admin"
    Write-Host "                  密码  : `${GF_SECURITY_ADMIN_PASSWORD}"
    Write-Host "    Alertmanager : http://localhost:9095"
    Write-Host ""
    Write-Host "${Bold}========================================${Reset}"
    Write-Host "  ${Yellow}常用命令${Reset}"
    Write-Host "${Bold}========================================${Reset}"
    Write-Host "    查看日志  : .\scripts\logs-prod.ps1"
    Write-Host "    停止服务  : .\scripts\stop-prod.ps1"
    Write-Host "    查看状态  : docker $script:ComposeArgs ps"
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
    Confirm-Start
    Invoke-PreflightChecks
    Invoke-SecurityChecks
    Load-EnvVars
    Start-Services
    $healthResult = Wait-ForHealth
    Invoke-DatabaseMigration
    Print-AccessInfo

    if ($healthResult) {
        Write-Host "${Green}${Bold}✓ 生产环境启动完成！${Reset}"
    } else {
        Write-Host "${Yellow}${Bold}! 生产环境已启动，但部分服务未完全就绪${Reset}"
        Write-Host "${Yellow}${Bold}! 请检查容器状态并确保所有服务正常运行${Reset}"
        exit 1
    }
    Write-Host ""
}

Main
