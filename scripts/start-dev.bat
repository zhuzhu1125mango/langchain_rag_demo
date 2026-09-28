@echo off
chcp 65001 >nul

REM LangChain RAG Demo - Development Environment Startup Script (Windows)
REM This script detects PowerShell 7 and redirects to the PowerShell script

REM Check if PowerShell 7 is available
where pwsh >nul 2>&1
if %ERRORLEVEL% equ 0 (
    REM PowerShell 7 found, execute the PS1 script
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dev.ps1" %*
    exit /b %ERRORLEVEL%
)

REM PowerShell 7 is required (PS1 scripts are not compatible with 5.1)
echo ========================================
echo   错误: PowerShell 7 (pwsh) 未找到
echo ========================================
echo 本项目脚本需要 PowerShell 7 及以上版本，请安装后重试。
echo 下载地址: https://github.com/PowerShell/PowerShell/releases
echo ========================================
exit /b 1
