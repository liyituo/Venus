param(
    [string]$Base = 'http://127.0.0.1:8001',
    [string]$CaFile = ''
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$serverPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$checkArgs = @((Join-Path $PSScriptRoot 'check_venus_server.py'), '--base', $Base)
if ($CaFile) { $checkArgs += @('--ca-file', $CaFile) }
& $serverPython @checkArgs
exit $LASTEXITCODE
