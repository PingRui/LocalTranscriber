$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $app '.venv\Scripts\python.exe'
$script = Join-Path $app 'transcribe.py'

Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '选择要进行本地转写的视频或音频（可多选）'
$dialog.Filter = '视频和音频|*.mp4;*.mov;*.mkv;*.avi;*.mp3;*.wav;*.m4a;*.aac;*.flac;*.webm|所有文件|*.*'
$dialog.Multiselect = $true

if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
    exit 0
}

$env:PYTHONUTF8 = '1'
& $python $script @($dialog.FileNames)
$exitCode = $LASTEXITCODE

Write-Host ''
if ($exitCode -eq 0) {
    Write-Host '转写完成。结果保存在原视频旁的“转写结果”文件夹。' -ForegroundColor Green
} else {
    Write-Host "转写失败，错误码：$exitCode" -ForegroundColor Red
}
Read-Host '按回车键关闭窗口'
exit $exitCode
