param(
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

if (-not $Python) {
    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        $Python = $venvPython
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw 'Python was not found. Run the installer or pass -Python explicitly.'
        }
        $Python = $pythonCommand.Source
    }
}

$pythonFiles = @(
    'app_config.py',
    'batch_clean.py',
    'domain_hotwords.py',
    'gui.pyw',
    'knowledge_space.py',
    'knowledge_worker.py',
    'knowledge_pipeline.py',
    'llm_client.py',
    'llm_repair.py',
    'model_provider_config.py',
    'model_manager.py',
    'source_context.py',
    'task_hotwords.py',
    'hotword_library.py',
    'hotword_suggestions.py',
    'transcribe.py',
    'trusted_pipeline.py',
    'whole_file_review.py'
)

Write-Host '1/3 Compile Python entrypoints...' -ForegroundColor Cyan
& $Python -m py_compile @pythonFiles
if ($LASTEXITCODE -ne 0) {
    throw 'Python compilation failed.'
}

Write-Host '2/3 Run automated tests...' -ForegroundColor Cyan
& $Python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    throw 'Automated tests failed.'
}

Write-Host '3/3 Check Git diff formatting...' -ForegroundColor Cyan
$git = Get-Command git.exe -ErrorAction SilentlyContinue
if ($git) {
    & $git.Source diff --check
    if ($LASTEXITCODE -ne 0) {
        throw 'Git diff formatting check failed.'
    }
} else {
    Write-Host 'Git was not found; skipped the diff formatting check.' -ForegroundColor Yellow
}

Write-Host 'Project verification passed.' -ForegroundColor Green
