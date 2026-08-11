@echo off
chcp 65001 >nul
set "APP=%~dp0"
set "PYTHON=%APP%.venv\Scripts\pythonw.exe"

if not exist "%PYTHON%" (
  echo 尚未安装 LocalTranscriber，请先双击“安装.cmd”。
  pause
  exit /b 1
)

start "" "%PYTHON%" "%APP%gui.pyw" %*
