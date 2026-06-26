#Requires -Version 7.0
<#
.SYNOPSIS
    LangChain RAG Demo - View Development Environment Logs

.DESCRIPTION
    PowerShell 7 optimized script to view development environment logs

.PARAMETER Service
    Specific service to view logs for (e.g., backend, frontend)

.EXAMPLE
    .\scripts\logs-dev.ps1
    .\scripts\logs-dev.ps1 -Service backend
.NOTES
    用法：./scripts/logs-dev.ps1 [[-Service] <服务名>]
#>

param(
    [Parameter(Position=0)]
    [string]$Service
)

$script:ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent
$script:ComposeFile = Join-Path $script:ScriptDir "../docker-compose.dev.yml"
$script:Services = @("backend", "frontend", "postgres", "minio", "milvus-standalone", "redis", "searxng", "prometheus", "grafana")

# ==============================================================================
# Color Definitions
# ==============================================================================

function Get-ANSI { param([string]$Code) { "`e[$Code" } }

$Reset = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "0m" } else { "" }
$Cyan = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "36m" } else { "" }
$Bold = if ($host.UI.SupportsVirtualTerminal) { Get-ANSI "1m" } else { "" }

# ==============================================================================
# Main
# ==============================================================================

Write-Host "${Bold}${Cyan}=== Development Environment Logs ===${Reset}"
Write-Host ""

# If specific service is requested
if ($Service) {
    Write-Host "Viewing logs for: $Service"
    Write-Host ""
    docker compose -f $script:ComposeFile logs -f --tail=200 $Service
} else {
    Write-Host "Viewing logs for all services (Ctrl+C to exit)"
    Write-Host ""
    Write-Host "Tip: Use '.\scripts\logs-dev.ps1 <service>' to view specific service logs"
    Write-Host "     Available services: $($script:Services -join ', ')"
    Write-Host ""
    docker compose -f $script:ComposeFile logs -f --tail=100
}
