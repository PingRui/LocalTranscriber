@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-agent.ps1"
if errorlevel 1 (
  echo.
  echo Agent 接入安装失败，请查看上方错误。
  pause
  exit /b 1
)
echo.
pause
