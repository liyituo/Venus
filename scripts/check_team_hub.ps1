param(
    [string]$HubHost = ""
)

$ErrorActionPreference = "Stop"
$tailscaleCommand = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $tailscaleCommand) {
    throw "找不到 tailscale 命令，请先安装并登录 Tailscale。"
}
$tailscaleExe = $tailscaleCommand.Source
$statusLines = & $tailscaleExe status --json
if ($LASTEXITCODE -ne 0) {
    throw "tailscale status --json 失败，请确认 Tailscale 在线。"
}
$tailnetJson = $statusLines -join [Environment]::NewLine
$tailnetStatus = $tailnetJson | ConvertFrom-Json
$detectedHost = ([string]$tailnetStatus.Self.DNSName).TrimEnd(".").ToLowerInvariant()
if (-not $HubHost) {
    $HubHost = $detectedHost
}
$HubHost = $HubHost.Trim().TrimEnd(".").ToLowerInvariant()
if ($HubHost -ne $detectedHost -or
        $HubHost -notmatch '^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.ts\.net$' -or
        $HubHost.Contains("..")) {
    throw "HubHost 必须是本机精确的 Tailscale MagicDNS 域名（*.ts.net）。检测到：$detectedHost"
}

$serveStatus = [string](& $tailscaleExe serve status 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "无法读取 Tailscale Serve 状态；请先运行 scripts/start_team_hub.ps1。"
}
$rootProxyPattern = '(?im)^\s*\|--\s+/\s+proxy\s+http://127\.0\.0\.1:8001\s*$'
if ($serveStatus -notmatch [regex]::Escape($HubHost) -or
        $serveStatus -notmatch $rootProxyPattern) {
    throw "Serve 根路径未确认转发到 Hub loopback；请检查 tailscale serve status。"
}

$httpsOrigin = "https://$HubHost"
$public = Invoke-RestMethod -Method Get `
    -Uri "$httpsOrigin/api/v1/team/public" -TimeoutSec 10
if (-not $public.ok -or $public.serve_host -ne $HubHost) {
    throw "HTTPS Hub 响应与配置主机名不匹配；请检查 Serve 和 Hub 启动参数。"
}
if (-not $public.initialized) {
    throw "Serve 已就绪：$httpsOrigin；团队尚未初始化，team_id 暂不可用。请先在 Hub 本机 VenusChat 初始化团队。"
}

Write-Host "团队 HTTPS 地址：$httpsOrigin"
Write-Host "团队名称：$($public.team_name)"
Write-Host "team_id：$($public.team_id)"
Write-Host "Hub：$($public.serve_host)（Tailscale Serve 根路径 → 127.0.0.1:8001）"
