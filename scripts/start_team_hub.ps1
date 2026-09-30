param(
    [string]$ListenAddress = '0.0.0.0',
    [ValidateRange(1, 65535)][int]$Port = 8001,
    [switch]$RequirePassword,
    [string]$TlsCert = '',
    [string]$TlsKey = '',
    [string[]]$PublicHost = @()
)
# Team authorization is independent from optional transport/password settings.
& (Join-Path $PSScriptRoot 'Start-VenusServer.ps1') -Team @PSBoundParameters
exit $LASTEXITCODE
