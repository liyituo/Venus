param(
    [string]$ListenAddress = '0.0.0.0',
    [ValidateRange(1, 65535)][int]$Port = 8001,
    [switch]$Team,
    [switch]$RequirePassword,
    [string]$TlsCert = '',
    [string]$TlsKey = '',
    [string[]]$PublicHost = @()
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$serverPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $serverPython)) {
    throw '请先创建 .venv 并安装 requirements.txt。'
}
if ([bool]$TlsCert -ne [bool]$TlsKey) {
    throw 'TLS 证书和私钥必须一起设置。'
}
$serverArgs = @((Join-Path $repoRoot 'src\llm_server.py'), '--host', $ListenAddress, '--port', [string]$Port)
if ($Team) { $serverArgs += '--team' }
if ($RequirePassword) { $serverArgs += '--password' }
if ($TlsCert) { $serverArgs += @('--tls-cert', $TlsCert, '--tls-key', $TlsKey) }
foreach ($allowedHost in $PublicHost) { $serverArgs += @('--public-host', $allowedHost) }
Write-Host "Venus 服务端：$ListenAddress`:$Port；按 Ctrl+C 停止。"
& $serverPython @serverArgs
exit $LASTEXITCODE
