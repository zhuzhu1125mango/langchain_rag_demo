@echo off
chcp 65001 >nul

REM LangChain RAG Demo - Production Environment Startup Script (Windows)
REM This script detects PowerShell 7 and redirects to the PowerShell script

REM Check if PowerShell 7 is available
where pwsh >nul 2>&1
if %ERRORLEVEL% equ 0 (
    REM PowerShell 7 found, execute the PS1 script
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-prod.ps1" %*
    exit /b %ERRORLEVEL%
)

REM Fallback to PowerShell 5.1 with warning
echo ========================================
echo   警告: PowerShell 7 未找到
echo ========================================
echo 建议安装 PowerShell 7 以获得更好的体验
echo 下载地址: https://github.com/PowerShell/PowerShell/releases
echo ========================================
echo 尝试使用 PowerShell 5.1 运行...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-prod.ps1" %*
exit /b %ERRORLEVEL%
