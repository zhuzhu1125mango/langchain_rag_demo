#Requires -Version 7.0
<#
.SYNOPSIS
    LangChain RAG Demo - Stop Development Environment

.DESCRIPTION
    PowerShell 7 optimized script to stop development environment

.PARAMETER Clean
    Stop services and remove volumes

.EXAMPLE
    .\scripts\stop-dev.ps1
    .\scripts\stop-dev.ps1 -Clean
.NOTES
    用法：./scripts/stop-dev.ps1 [-Clean]
#>

param(
    [switch]$Clean
)

$script:ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent
$script:ComposeFile = Join-Path $script:ScriptDir "../docker-compose.dev.yml"

# ==============================================================================
# Color Definitions
# ==============================================================================

function Get-ANSI { param([string]$Code) { "`e[$Code" } }

$Reset = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "0m" } else { "" }
$Red = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "31m" } else { "" }
$Green = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "32m" } else { "" }
$Yellow = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "33m" } else { "" }
$Cyan = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "36m" } else { "" }
$Bold = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "1m" } else { "" }

function Write-Info { Write-Host "${Blue}[INFO]${Reset} $args" }
function Write-OK { Write-Host "${Green}[OK]${Reset} $args" }
function Write-Warn { Write-Host "${Yellow}[WARN]${Reset} $args" }
function Write-Err { Write-Host "${Red}[ERROR]${Reset} $args" }

# ==============================================================================
# Main
# ==============================================================================

Write-Host ""
Write-Host "${Bold}========================================${Reset}"
Write-Host "${Bold}  停止开发环境${Reset}"
Write-Host "${Bold}========================================${Reset}"
Write-Host ""

if ($Clean) {
    Write-Warn "警告: 将删除所有数据卷！"
    $response = Read-Host "确认继续? [y/N]: "
    Write-Host ""

    if ($response -notmatch "^[Yy]$") {
        Write-Info "已取消"
        exit 0
    }

    Write-Host "停止服务并删除数据卷..."
    docker compose -f $script:ComposeFile down -v
    Write-Host ""
    Write-OK "开发环境已停止，数据卷已删除"
} else {
    Write-Host "停止服务..."
    docker compose -f $script:ComposeFile down
    Write-Host ""
    Write-OK "开发环境已停止"
    Write-Host ""
    Write-Info "提示: 使用 '.\scripts\stop-dev.ps1 -Clean' 可同时删除数据卷"
}

Write-Host ""
