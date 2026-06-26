#Requires -Version 7.0
<#
.SYNOPSIS
    LangChain RAG Demo - Stop Production Environment

.DESCRIPTION
    PowerShell 7 optimized script to stop production environment

.PARAMETER Clean
    Stop services and remove volumes

.EXAMPLE
    .\scripts\stop-prod.ps1
    .\scripts\stop-prod.ps1 -Clean
.NOTES
    用法：./scripts/stop-prod.ps1 [-Clean]
#>

param(
    [switch]$Clean
)

$script:ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent
$script:ComposeFile = Join-Path $script:ScriptDir "../docker-compose.yml"

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
Write-Host "${Bold}  停止生产环境${Reset}"
Write-Host "${Bold}========================================${Reset}"
Write-Host ""
Write-Warn "⚠ 警告: 这是生产环境"
Write-Host ""

if ($Clean) {
    Write-Err "严重警告: 将删除所有生产数据！"
    Write-Host "这包括数据库、向量存储、对象存储中的所有数据！"
    Write-Host ""
    $response = Read-Host "输入 'DELETE' 确认删除所有数据: "
    Write-Host ""

    if ($response -ne "DELETE") {
        Write-Info "已取消"
        exit 0
    }

    Write-Host "停止服务并删除数据卷..."
    docker compose -f $script:ComposeFile down -v
    Write-Host ""
    Write-OK "生产环境已停止，数据卷已删除"
} else {
    Write-Host "停止服务..."
    docker compose -f $script:ComposeFile down
    Write-Host ""
    Write-OK "生产环境已停止"
    Write-Host ""
    Write-Info "提示: 使用 '.\scripts\stop-prod.ps1 -Clean' 可同时删除数据卷（危险操作）"
}

Write-Host ""
