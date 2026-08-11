param(
    [ValidateSet('', 'medium', 'large-v3-turbo')]
    [string]$Model = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

function Resolve-PythonExecutable {
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($selector in @('-3.12', '-3.11', '-3.10', '-3.9')) {
            $resolved = & $pyLauncher.Source $selector -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                return $resolved.Trim()
            }
        }
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return $pythonCommand.Source
    }
    throw '未检测到 Python。请先安装 Python 3.9–3.12，并勾选 Add Python to PATH。'
}

Write-Host ''
Write-Host 'LocalTranscriber 安装与环境检查' -ForegroundColor Cyan
Write-Host '=================================' -ForegroundColor DarkGray

if ($env:OS -ne 'Windows_NT') {
    throw '当前桌面版安装向导仅支持 Windows 10/11。'
}
Write-Host '[通过] Windows 环境' -ForegroundColor Green

$pythonExe = Resolve-PythonExecutable
$pythonVersion = & $pythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3]))); raise SystemExit(0 if (3, 9) <= sys.version_info[:2] <= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 版本不受支持：$pythonVersion。请使用 Python 3.9–3.12。"
}
Write-Host "[通过] Python $pythonVersion" -ForegroundColor Green

$memoryBytes = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
$memoryGB = [math]::Round($memoryBytes / 1GB, 1)
$localDataRoot = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $projectRoot }
$driveName = ([System.IO.Path]::GetPathRoot($localDataRoot)).TrimEnd('\').TrimEnd(':')
$freeGB = [math]::Round((Get-PSDrive -Name $driveName).Free / 1GB, 1)
Write-Host "[信息] 内存：$memoryGB GB；模型盘可用空间：$freeGB GB"
if ($freeGB -lt 3) {
    throw '模型盘可用空间不足 3GB，请释放空间后重试。'
}

$webViewPaths = @(
    "${env:ProgramFiles(x86)}\Microsoft\EdgeWebView\Application",
    "$env:ProgramFiles\Microsoft\EdgeWebView\Application"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
if ($webViewPaths.Count -gt 0) {
    Write-Host '[通过] Microsoft Edge WebView2' -ForegroundColor Green
} else {
    Write-Warning '未检测到 WebView2。安装完成后若界面无法打开，请安装 Microsoft Edge WebView2 Runtime。'
}

$gpuDetected = $false
$gpuDescription = ''
$nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if ($nvidiaSmi) {
    $gpuOutput = @(& $nvidiaSmi.Source --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>$null)
    $gpuDescription = $gpuOutput | Select-Object -First 1
    if ($gpuDescription) {
        $gpuDetected = $true
        Write-Host "[通过] NVIDIA GPU：$gpuDescription" -ForegroundColor Green
        Write-Host '[提示] GPU 加速还需要 CUDA 12 与 cuDNN 9；缺失时程序会自动回退 CPU。'
    }
}
if (-not $gpuDetected) {
    Write-Host '[信息] 未检测到可用的 NVIDIA GPU，将使用 CPU 模式。'
}

$recommendedModel = if ($gpuDetected) { 'large-v3-turbo' } else { 'medium' }
if (-not $Model) {
    Write-Host ''
    Write-Host "请选择本地语音模型（推荐：$recommendedModel）：" -ForegroundColor Cyan
    Write-Host '  1. Medium            约 1.43GB，CPU 用户推荐'
    Write-Host '  2. Large-v3 Turbo    约 1.51GB，准确率更高，NVIDIA GPU 用户推荐'
    $selection = Read-Host '输入 1 或 2'
    $Model = switch ($selection.Trim()) {
        '1' { 'medium' }
        '2' { 'large-v3-turbo' }
        default { $recommendedModel }
    }
}
Write-Host "[选择] $Model" -ForegroundColor Cyan

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host '正在创建 Python 虚拟环境…'
    & $pythonExe -m venv (Join-Path $projectRoot '.venv')
}

Write-Host '正在安装程序依赖…'
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    throw '依赖安装失败，请检查网络连接后重试。'
}

Write-Host '正在准备本地模型…'
& $venvPython (Join-Path $projectRoot 'model_manager.py') install --model $Model
if ($LASTEXITCODE -ne 0) {
    throw '模型下载失败，请检查网络和磁盘空间后重试。'
}

$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $desktop '本地语音转写.lnk'))
    $shortcut.TargetPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
    $shortcut.Arguments = '"' + (Join-Path $projectRoot 'gui.pyw') + '"'
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.IconLocation = Join-Path $projectRoot 'assets\localtranscriber-icon.ico'
    $shortcut.Save()
    Write-Host '[完成] 已创建桌面快捷方式：本地语音转写' -ForegroundColor Green
}

Write-Host ''
Write-Host '安装完成。模型和运行配置保存在当前 Windows 用户目录，不会写入源码仓库。' -ForegroundColor Green
Write-Host '双击“开始本地转写.cmd”或桌面快捷方式即可启动。'
