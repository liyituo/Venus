param(
    [switch]$InstallShortcut,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$pythonwExe = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
$serverFile = Join-Path $repoRoot 'src\llm_server.py'
$guiModule = Join-Path $repoRoot 'src\venuschat_v1\__main__.py'
$probeFile = Join-Path $PSScriptRoot 'probe_venus_backend.py'
$launcherScript = Join-Path $PSScriptRoot 'Start-VenusChat.ps1'
$iconFile = Join-Path $repoRoot 'assets\venuschat.ico'
$markerFile = Join-Path $repoRoot '.venus\venuschat-gui.json'
$logDir = Join-Path $repoRoot '.venus\launcher'

function Install-VenusShortcut {
    if (-not (Test-Path -LiteralPath $iconFile)) {
        throw "找不到图标：$iconFile"
    }
    $desktop = [Environment]::GetFolderPath('DesktopDirectory')
    if (-not $desktop) {
        throw '无法定位当前用户的桌面。'
    }
    $link = Join-Path $desktop 'VenusChat.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcherScript`""
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.IconLocation = "$iconFile,0"
    $shortcut.Description = '启动 VenusChat 桌面端和个人后端'
    $shortcut.Save()
    Write-Host "桌面快捷方式已创建：$link"
}

function Get-GuiProcess {
    if (-not (Test-Path -LiteralPath $markerFile)) { return $null }
    try {
        $marker = Get-Content -Raw -LiteralPath $markerFile | ConvertFrom-Json
        $proc = Get-Process -Id ([int]$marker.pid) -ErrorAction Stop
        if ($proc.ProcessName -notin @('python', 'pythonw')) { return $null }
        $written = [DateTimeOffset]::Parse([string]$marker.started_at).UtcDateTime
        if ([Math]::Abs(($proc.StartTime.ToUniversalTime() - $written).TotalSeconds) -gt 15) {
            return $null
        }
        return $proc
    } catch {
        return $null
    }
}

function Show-GuiProcess($proc) {
    try {
        $typeDefinition = @'
using System;
using System.Runtime.InteropServices;
public static class VenusWindow {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
'@
        Add-Type -TypeDefinition $typeDefinition -ErrorAction Stop
        $proc.Refresh()
        if ($proc.MainWindowHandle -ne [IntPtr]::Zero) {
            [VenusWindow]::ShowWindowAsync($proc.MainWindowHandle, 9) | Out-Null
            [VenusWindow]::SetForegroundWindow($proc.MainWindowHandle) | Out-Null
        }
    } catch {
        # The existing window can still be selected from the taskbar.
    }
}

function Get-BackendState {
    & $pythonExe $probeFile --base $baseUrl --timeout 3 *> $null
    if ($LASTEXITCODE -eq 0) { return 'ready' }
    if ($LASTEXITCODE -eq 1) { return 'down' }
    return 'other'
}

function Test-LocalPortOpen([int]$port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.ConnectAsync('127.0.0.1', $port)
        return ($connect.Wait(600) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Save-PersonalBackendConfig([string]$personalUrl, [string]$hubLocalUrl) {
    $env:VENUS_REPO_ROOT = $repoRoot
    $env:VENUS_PERSONAL_BACKEND_BASE = $personalUrl
    $env:VENUS_TEAM_HUB_LOCAL_BASE = $hubLocalUrl
    $saveCode = 'import os,sys; sys.path.insert(0,os.path.join(os.environ["VENUS_REPO_ROOT"],"src")); from venuschat_v1.config_store import save_local_config; updates={"personal_llm_base":os.environ["VENUS_PERSONAL_BACKEND_BASE"]}; hub=os.environ["VENUS_TEAM_HUB_LOCAL_BASE"]; updates.update({"team_hub_local_base":hub} if hub else {}); save_local_config(updates)'
    & $pythonExe -c $saveCode *> $null
    $saveStatus = $LASTEXITCODE
    Remove-Item Env:VENUS_REPO_ROOT, Env:VENUS_PERSONAL_BACKEND_BASE, Env:VENUS_TEAM_HUB_LOCAL_BASE -ErrorAction SilentlyContinue
    if ($saveStatus -ne 0) {
        Write-Host '无法保存个人后端地址；本次 GUI 仍会打开。' -ForegroundColor Yellow
        return $false
    }
    return $true
}

function Test-LocalVenusHub([int]$port) {
    try {
        $info = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:$port/api/v1/team/public" `
            -TimeoutSec 2 -ErrorAction Stop
        return ($null -ne $info.PSObject.Properties['initialized'])
    } catch {
        return $false
    }
}

function Ensure-PersonalBackend {
    $hubLocalUrl = ''
    $personalPort = 8001
    try {
        if (Test-Path -LiteralPath $configFile) {
            $savedConfig = Get-Content -Raw -LiteralPath $configFile | ConvertFrom-Json
            $configuredHubLocal = ([string]$savedConfig.team_hub_local_base).TrimEnd('/')
            if ($configuredHubLocal -eq 'http://127.0.0.1:8001') {
                $hubLocalUrl = $configuredHubLocal
            }
        }
    } catch { }
    if ($hubLocalUrl -or (Test-LocalVenusHub 8001)) {
        $hubLocalUrl = 'http://127.0.0.1:8001'
        $personalPort = 8002
        Write-Host '检测到本机 8001 正在提供团队 Hub；个人后端将使用 8002，保持两端隔离。'
    }
    $personalUrl = "http://127.0.0.1:$personalPort"
    & $pythonExe $probeFile --base $personalUrl --timeout 3 *> $null
    if ($LASTEXITCODE -eq 0) {
        if (Test-LocalVenusHub $personalPort) {
            Write-Host "个人端口 $personalPort 也在提供团队 Hub；不会将其当作个人后端。" -ForegroundColor Yellow
            return $false
        }
        [void](Save-PersonalBackendConfig $personalUrl $hubLocalUrl)
        return $true
    }
    if (Test-LocalPortOpen $personalPort) {
        Write-Host "本机 $personalPort 端口已有服务但未通过 Venus 个人后端检查；不会覆盖该服务。" -ForegroundColor Yellow
        return $false
    }
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $stdoutLog = Join-Path $logDir "backend-$personalPort.out.log"
    $stderrLog = Join-Path $logDir "backend-$personalPort.err.log"
    $serverArgs = "-u `"$serverFile`" --host 127.0.0.1 --port $personalPort"
    $backend = Start-Process -FilePath $pythonExe -ArgumentList $serverArgs `
        -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    $deadline = [DateTime]::UtcNow.AddSeconds(75)
    do {
        Start-Sleep -Milliseconds 700
        & $pythonExe $probeFile --base $personalUrl --timeout 3 *> $null
        if ($LASTEXITCODE -eq 0) {
            [void](Save-PersonalBackendConfig $personalUrl $hubLocalUrl)
            return $true
        }
        $backend.Refresh()
        if ($backend.HasExited) { break }
    } while ([DateTime]::UtcNow -lt $deadline)
    Write-Host "个人后端未能启动；请查看 $stderrLog。" -ForegroundColor Yellow
    return $false
}

try {
    if (-not (Test-Path -LiteralPath $serverFile) -or
        -not (Test-Path -LiteralPath $guiModule) -or
        -not (Test-Path -LiteralPath $probeFile)) {
        throw 'VenusChat 源文件不完整；请确认从项目的 scripts 目录运行。'
    }

    if ($InstallShortcut) { Install-VenusShortcut }
    if ($NoLaunch) { exit 0 }

    Set-Location -LiteralPath $repoRoot
    if (-not (Test-Path -LiteralPath $pythonExe) -or
        -not (Test-Path -LiteralPath $pythonwExe)) {
        Write-Host '[1/4] 创建 Python 虚拟环境…'
        $launcher = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' }
                    elseif (Get-Command python -ErrorAction SilentlyContinue) { 'python' }
                    else { throw '未找到 Python 3。请先安装 Python 3，然后重新双击启动脚本。' }
        if ($launcher -eq 'py') {
            & py -3 -m venv (Join-Path $repoRoot '.venv')
        } else {
            & python -m venv (Join-Path $repoRoot '.venv')
        }
        if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败。' }
    }

    & $pythonExe -c 'import fastapi, uvicorn, PIL, pyautogui, fastmcp, yaml' *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[2/4] 安装项目依赖（首次运行可能需要几分钟）…'
        & $pythonExe -m pip install -r (Join-Path $repoRoot 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络或 pip 输出。' }
    }

    $baseUrl = 'http://127.0.0.1:8001'
    $configFile = Join-Path $repoRoot 'chat_config.json'
    if (Test-Path -LiteralPath $configFile) {
        try {
            $config = Get-Content -Raw -LiteralPath $configFile | ConvertFrom-Json
            if ($config.llm_base) { $baseUrl = ([string]$config.llm_base).TrimEnd('/') }
        } catch {
            Write-Host '配置文件无法读取，将使用默认本机后端地址。' -ForegroundColor Yellow
        }
    }
    $existingGui = Get-GuiProcess
    if ($null -ne $existingGui) {
        Show-GuiProcess $existingGui
        Write-Host 'VenusChat 已在运行，已尝试切换到现有窗口。'
        exit 0
    }
    $backendUri = [Uri]$baseUrl
    $localBackend = ($backendUri.Scheme -eq 'http' -and
        $backendUri.Host -in @('127.0.0.1', 'localhost') -and
        $backendUri.AbsolutePath -eq '/')
    $port = $backendUri.Port
    $reservedHubLocal = $false
    if (Test-Path -LiteralPath $configFile) {
        try {
            $reservedHubLocal = (([string]$config.team_hub_local_base).TrimEnd('/') -eq
                $baseUrl.TrimEnd('/'))
        } catch { }
    }

    Write-Host "[3/4] 检查后端：$baseUrl"
    $backendState = Get-BackendState
    if ($backendState -eq 'other') {
        if ($localBackend) {
            throw "地址 $baseUrl 有响应，但不是可用的 Venus 后端（或凭证不匹配）；不会覆盖现有服务。"
        }
        Write-Host "远端 Hub $baseUrl 响应异常或凭证失效，将打开 VenusChat；可用聊天侧栏的空间切换按钮切回个人对话。" -ForegroundColor Yellow
    }
    if ($backendState -eq 'down') {
        if ((-not $localBackend) -or $reservedHubLocal) {
            if ($reservedHubLocal) {
                Write-Host "保留的本机 Hub 地址 $baseUrl 当前离线；不会占用 8001 启动个人后端，将检查独立个人端口。" -ForegroundColor Yellow
            } else {
                Write-Host "远端 Hub $baseUrl 当前不可达，将打开 VenusChat；可用聊天侧栏的空间切换按钮切回个人对话。" -ForegroundColor Yellow
            }
        } else {
            if (Test-LocalPortOpen $port) {
                throw "127.0.0.1:$port 已被其他进程占用或服务未响应；不会启动第二个后端。"
            }
            New-Item -ItemType Directory -Force -Path $logDir | Out-Null
            $stdoutLog = Join-Path $logDir 'backend.out.log'
            $stderrLog = Join-Path $logDir 'backend.err.log'
            $serverArgs = "-u `"$serverFile`" --host 127.0.0.1 --port $port"
            $backend = Start-Process -FilePath $pythonExe -ArgumentList $serverArgs `
                -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
            Write-Host "后端启动中，日志：$stderrLog"
            $deadline = [DateTime]::UtcNow.AddSeconds(75)
            do {
                Start-Sleep -Milliseconds 700
                $backendState = Get-BackendState
                if ($backendState -eq 'ready') { break }
                $backend.Refresh()
                if ($backend.HasExited) {
                    throw "后端启动失败（退出码 $($backend.ExitCode)）。请查看 $stderrLog；团队 Hub 请使用 scripts\start_team_hub.ps1。"
                }
            } while ([DateTime]::UtcNow -lt $deadline)
            if ($backendState -ne 'ready') {
                throw "后端未在 75 秒内就绪。请查看 $stderrLog。"
            }
        }
    }
    if ((-not $localBackend) -or $reservedHubLocal -or (Test-LocalVenusHub $port)) {
        # The user may switch to personal chat even when the selected remote
        # Hub is healthy, so ensure the local personal endpoint is ready too.
        [void](Ensure-PersonalBackend)
    }
    Write-Host '后端检查完成。'

    Write-Host '[4/4] 打开 VenusChat…'

    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $guiStdout = Join-Path $logDir 'gui.out.log'
    $guiStderr = Join-Path $logDir 'gui.err.log'
    $gui = Start-Process -FilePath $pythonwExe -ArgumentList '-m venuschat_v1' `
        -WorkingDirectory (Join-Path $repoRoot 'src') -WindowStyle Normal -PassThru `
        -RedirectStandardOutput $guiStdout -RedirectStandardError $guiStderr
    $guiDeadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        if ($null -ne (Get-GuiProcess)) {
            Write-Host 'VenusChat 已启动。'
            exit 0
        }
        $gui.Refresh()
        if ($gui.HasExited) { break }
    } while ([DateTime]::UtcNow -lt $guiDeadline)
    throw "GUI 未能启动。请查看 $guiStderr。"
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
