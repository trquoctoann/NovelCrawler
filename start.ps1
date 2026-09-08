param(
    [ValidateSet('run', 'once', 'status', 'test', 'demo', 'sources', 'background', 'stop')]
    [string]$Mode = 'background'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    Write-Host 'Preparing local Python environment (first run only)...'
    $taskBootstrap = (Get-Command python -ErrorAction Stop).Source
    & $taskBootstrap -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
    & $taskBootstrap -m venv --system-site-packages (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
}
& $taskPython -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ('httpx', 'bs4')) else 1)"
if ($LASTEXITCODE -ne 0) {
    & $taskPython -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your internet connection.' }
}
switch ($Mode) {
    'background' { & $taskPython -m scripts.background }
    'stop' { & $taskPython -m scripts.background stop }
    'test' {
        & $taskPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pytest') else 1)"
        if ($LASTEXITCODE -ne 0) {
            & $taskPython -m pip install 'pytest>=8,<10'
            if ($LASTEXITCODE -ne 0) { throw 'Test dependency installation failed.' }
        }
        & $taskPython -m pytest -q
    }
    'demo' { & $taskPython -m scripts.demo }
    'status' { & $taskPython -m novel_crawler status }
    'once' { & $taskPython -m novel_crawler start --once }
    'sources' { & $taskPython -m novel_crawler sources }
    default { & $taskPython -m novel_crawler start }
}
exit $LASTEXITCODE
