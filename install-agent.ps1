param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw '尚未找到项目虚拟环境。请先运行“安装.cmd”，再安装 Agent 接入。'
}

$version = & $venvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) {
    throw '无法读取项目 Python 版本。'
}
$parts = $version.Trim().Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 10)) {
    throw "Agent MCP 接入需要 Python 3.10 或更高版本；当前项目环境是 Python $version。"
}

Write-Host '正在安装本地 Agent MCP 接入…' -ForegroundColor Cyan
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements-mcp.txt')
if ($LASTEXITCODE -ne 0) {
    throw 'Agent MCP 依赖安装失败。'
}

& $venvPython -c "import localtranscriber_mcp; print('MCP_READY')"
if ($LASTEXITCODE -ne 0) {
    throw 'Agent MCP 服务导入检查失败。'
}
Write-Host 'Agent 接入已安装。重新打开 LocalTranscriber 后，可在“Agent 接入”页复制配置。' -ForegroundColor Green
