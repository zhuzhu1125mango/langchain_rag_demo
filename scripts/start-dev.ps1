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
$script:EnvFile = Join-Path $script:ScriptDir "../.env.dev"
$script:ComposeFile = Join-Path $script:ScriptDir "../docker-compose.dev.yml"
$script:ComposeArgs = @("compose", "-f", $script:ComposeFile, "--env-file", $script:EnvFile)
$script:EnvName = "DEVELOPMENT"
# 精简模式（P0-3）：默认 7 个核心服务；$env:INCLUDE_MONITORING = "1" 时带监控栈
$script:ProfileArgs = @()
if ($env:INCLUDE_MONITORING -eq "1") { $script:ProfileArgs = @("--profile", "monitoring") }
# 默认 240s：干净机器首次启动要等镜像构建 + 模型预热，实测 backend 到 healthy 约 3~4 分钟
$script:MaxWaitSeconds = $env:MAX_WAIT_SECONDS ? [int]$env:MAX_WAIT_SECONDS : 240
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
    Write-Step "1/5" "检测运行环境..."

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
    if (Test-Path $script:EnvFile) {
        Write-OK "配置文件 $script:EnvFile 存在"
    } else {
        # 首次运行自动生成：全新 clone 里没有 .env.dev（.gitignore 排除 .env*），
        # 而 README 承诺「10 分钟上手」——让用户自己去凑 189 个键，这个承诺就不成立。
        # 只有 dev 这么干：生产必须由人显式配置强密码，脚本不替它猜。
        $exampleFile = Join-Path $script:ScriptDir "../.env.example"
        if (-not (Test-Path $exampleFile)) {
            Write-Err "既没有 $script:EnvFile 也没有 .env.example，无法自动生成配置"
            exit 1
        }
        Copy-Item $exampleFile $script:EnvFile

        # SECRET_KEY 必须生成：留空时签发 JWT 会抛 ValueError（auth.py），
        # 表现是「注册/登录直接失败」，而 .env.example 里它本来就是空值。
        $chars = (48..57) + (65..90) + (97..122)
        $secret = -join (1..64 | ForEach-Object { [char]($chars | Get-Random) })
        $content = Get-Content $script:EnvFile -Raw -Encoding UTF8
        $content = [regex]::Replace($content, '(?m)^SECRET_KEY=.*$', "SECRET_KEY=$secret")
        Set-Content -Path $script:EnvFile -Value $content -Encoding UTF8 -NoNewline
        Write-OK "已从 .env.example 生成 $script:EnvFile，并随机生成 SECRET_KEY"
        Write-Info "如需改端口/密码，编辑 $script:EnvFile 后重跑本脚本"
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
# Load Environment Variables
# ==============================================================================

function Load-EnvVars {
    Write-Step "2/5" "加载环境变量..."

    # Load .env.dev file（用于脚本内展示；compose 经 --env-file 自行读取同一文件）
    Get-Content $script:EnvFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            # 剥析行内注释（"　#" 前缀的说明文字），避免展示/注入到含注释的值
            if ($value -match '\s+#') {
                $value = ($value -replace '\s+#.*$', '').Trim()
            }
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
    Write-Step "3/5" "启动 Docker Compose 服务..."

    # CHECK_ONLY=1：只跑预检（配置文件、compose 解析、服务清单），不起容器。
    # 用于「别人能不能跑起来」的复现验证与 CI——否则验证这一步要真拉一遍镜像。
    if ($env:CHECK_ONLY -eq "1") {
        Write-Info "CHECK_ONLY=1，仅校验配置，不启动容器"
        $services = & docker @script:ComposeArgs @script:ProfileArgs config --services 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Err "compose 解析失败："
            $services | ForEach-Object { Write-Host "    $_" }
            exit 1
        }
        $list = @($services | Where-Object { $_ -and $_.Trim() })
        Write-OK "compose 解析通过，服务数 $($list.Count)："
        $list | ForEach-Object { Write-Host "    - $_" }
        exit 0
    }

    Write-Info "拉取/构建镜像中，请耐心等待..."
    Write-Host ""

    & docker @script:ComposeArgs @script:ProfileArgs up -d --build
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
    Write-Step "4/5" "等待服务健康检查..."

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

    Write-Host ""
    return $allHealthy
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
    Write-Step "5/5" "输出访问信息..."
    Write-Host ""

    # Read env vars for display (dev mode shows actual values；不展示敏感密码)
    $pgDb = $env:POSTGRES_DB ?? "rag_demo"
    $pgUser = $env:POSTGRES_USER ?? "postgres"
    $minioUser = $env:MINIO_ROOT_USER ?? "minioadmin"
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
    Write-Host "    MinIO 控制台 : http://localhost:9001"
    Write-Host "                  用户名: $minioUser"
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
    Write-Host "    查看状态  : docker $script:ComposeArgs ps"
    Write-Host "    进入容器  : docker exec -it <container_name> powershell"
    Write-Host ""
    Write-Host "${Bold}========================================${Reset}"
    Write-Host ""
}

# ==============================================================================
# Main
# ==============================================================================

function Test-OllamaModels {
    # Ollama 不在 compose 里（本机安装），后端启动时会校验模型是否存在，缺失即拒绝启动。
    # 不预先检查的话，用户看到的是「7 个容器起来了但 backend unhealthy」，
    # 而真实原因是模型没拉——这一步只是把那个报错提前成一句人话。
    if ($env:CHECK_ONLY -eq "1") {
        Write-Info "CHECK_ONLY=1，跳过 Ollama 模型检查"
        return
    }
    if ($env:SKIP_OLLAMA_CHECK -eq "1") {
        Write-Warn "SKIP_OLLAMA_CHECK=1，跳过模型检查（缺失将导致后端启动失败）"
        return
    }

    $map = @{}
    foreach ($line in Get-Content $script:EnvFile -Encoding UTF8) {
        if ($line -match '^(EMBEDDING_MODEL_NAME|OLLAMA_DIRECT_MODEL_NAME|OLLAMA_MODEL_NAME)=(.+)$') {
            # 去掉行内注释：# .env.example 里写的是 `OLLAMA_MODEL_NAME=qwen3:4b  # 说明`，
            # 不去注释会把说明文字当成模型名，结果是「模型明明装了却报缺失」
            $map[$matches[1]] = ($matches[2].Split('#')[0]).Trim().Trim('"')
        }
    }
    $wanted = @($map.Values | Where-Object { $_ })

    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Err "未找到 ollama 命令：Ollama 不在 compose 内，需要单独安装并启动"
        Write-Info "安装：https://ollama.com/download"
        Write-Info "装好后拉取模型（见 README「上手」一节），或设 `$env:SKIP_OLLAMA_CHECK=1 跳过本检查"
        exit 1
    }

    $tags = & ollama list 2>$null | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] }
    if ($LASTEXITCODE -ne 0) {
        Write-Err "无法读取 ollama 模型列表（Ollama 服务可能没启动）"
        Write-Info "先执行 ollama serve，或检查 Docker Desktop 内 Ollama 是否运行"
        exit 1
    }

    $missing = @()
    foreach ($want in $wanted) {
        $base = ($want -split ':')[0]   # 忽略 tag 差异：bge-m3 与 bge-m3:latest 视为同一个
        if (-not ($tags -contains $want -or $tags -contains "$base`:latest" -or $tags -contains $base)) {
            $missing += $want
        }
    }

    if ($missing.Count -gt 0) {
        Write-Err "缺少 Ollama 模型：$($missing -join ', ')"
        Write-Info "按下面几条补齐（约 5.6GB，首次下载较慢）："
        $missing | ForEach-Object { Write-Host "    ollama pull $_" }
        Write-Info "只想先看配置对不对，可用 `$env:CHECK_ONLY=1 跳过启动"
        exit 1
    }
    Write-OK "Ollama 模型齐全：$($wanted -join ', ')"
}

function Main {
    Print-Banner
    Invoke-PreflightChecks
    Load-EnvVars
    Test-OllamaModels
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
