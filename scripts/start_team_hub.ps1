param(
    [string]$HubHost = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "找不到 .venv\Scripts\python.exe 或可用的 python。"
    }
    $pythonExe = $pythonCommand.Source
}

$tailscaleCommand = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $tailscaleCommand) {
    throw "找不到 tailscale 命令，请先安装并登录 Tailscale。"
}
$tailscaleExe = $tailscaleCommand.Source
$statusText = & $tailscaleExe status --json
if ($LASTEXITCODE -ne 0) {
    throw "tailscale status --json 失败，请确认 Tailscale 在线。"
}
$tailnetJson = $statusText -join [Environment]::NewLine
$tailnetStatus = $tailnetJson | ConvertFrom-Json
$detectedHost = ([string]$tailnetStatus.Self.DNSName).TrimEnd(".").ToLowerInvariant()
if (-not $HubHost) {
    $HubHost = $detectedHost
}
$HubHost = $HubHost.Trim().TrimEnd(".").ToLowerInvariant()
if ($HubHost -notmatch '^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.ts\.net$' -or $HubHost.Contains("..")) {
    throw "HubHost 必须是精确的 Tailscale MagicDNS HTTPS 域名（*.ts.net），当前值：$HubHost"
}
if ($detectedHost -and $HubHost -ne $detectedHost) {
    throw "HubHost 与本机 Tailscale DNS 名不一致。检测到 $detectedHost，输入了 $HubHost。"
}

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 8001)
try {
    $listener.Start()
    $listener.Stop()
} catch {
    try { $listener.Stop() } catch { }
    throw "127.0.0.1:8001 已被占用。请先确认占用程序；本脚本不会停止或覆盖已有进程。"
}

$arguments = @(
    "src\llm_server.py",
    "--host", "127.0.0.1",
    "--port", "8001",
    "--isolated",
    "--team-serve",
    "--team-host", $HubHost
)
$server = Start-Process -FilePath $pythonExe -ArgumentList $arguments `
    -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    if ($server.HasExited) {
        throw "Venus Hub 启动失败，进程退出码：$($server.ExitCode)。请检查 .venus/server.log。"
    }
    try {
        $probe = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8001/api/v1/team/public" -TimeoutSec 2
        if ($probe.ok -and ($probe.initialized -eq $true -or $probe.initialized -eq $false)) {
            $ready = $true
            break
        }
    } catch {
        # Wait for the local-only backend to finish binding.
    }
}
if (-not $ready) {
    if (-not $server.HasExited) {
        Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
    }
    throw "Venus Hub 未在 15 秒内通过本机 Serve 配置检查。"
}

Write-Host "Hub 后端只监听 127.0.0.1:8001，隔离模式已启用。"
if ($probe.initialized) {
    Write-Host "团队：$($probe.team_name)"
    Write-Host "team_id：$($probe.team_id)"
} else {
    Write-Host "团队：尚未初始化。请在这台 Hub 电脑的 VenusChat → 设置 → 团队与成员中初始化。"
}
$existingServe = [string](& $tailscaleExe serve status 2>&1)
$existingServeExit = $LASTEXITCODE
$rootProxyPattern = '(?im)^\s*\|--\s+/\s+proxy\s+http://127\.0\.0\.1:8001\s*$'
$alreadyConfigured = ($existingServe -match [regex]::Escape($HubHost)) -and
    ($existingServe -match $rootProxyPattern)
$noServePattern = '(?i)no serve config|no servers? are serving|not serving'
$hasOtherServe = ($existingServeExit -eq 0) -and $existingServe.Trim() -and
    ($existingServe -notmatch $noServePattern) -and (-not $alreadyConfigured)
if ($hasOtherServe) {
    if (-not $server.HasExited) {
        Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
    }
    throw "本机已有其他 Tailscale Serve 配置；为避免覆盖它，本脚本未修改配置。请先查看 tailscale serve status，并手动为 $HubHost 的根路径配置 http://127.0.0.1:8001。"
}
if (-not $alreadyConfigured) {
    Write-Host "正在配置 Tailscale Serve HTTPS（tailnet 内可访问，不启用 Funnel）。"
    & $tailscaleExe serve --bg --https=443 "http://127.0.0.1:8001"
    if ($LASTEXITCODE -ne 0) {
        if (-not $server.HasExited) {
            Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
        }
        throw "Tailscale Serve 配置失败。后端仍只绑定本机；请检查 tailscale serve status。"
    }
}
$serveStatus = [string](& $tailscaleExe serve status 2>&1)
if ($LASTEXITCODE -ne 0 -or $serveStatus -notmatch [regex]::Escape($HubHost) -or
        $serveStatus -notmatch $rootProxyPattern) {
    if (-not $server.HasExited) {
        Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
    }
    throw "Tailscale Serve 状态未确认 https://$HubHost/ → http://127.0.0.1:8001。请检查 tailscale serve status；脚本不会开放后端监听地址。"
}
Write-Host "已确认团队 HTTPS 地址：https://$HubHost"
Write-Host "Tailscale Serve 状态："
Write-Host $serveStatus
Write-Host "设备重启后 Serve 会在后台恢复。查看状态：tailscale serve status；停用此根路径：tailscale serve --https=443 off。"
