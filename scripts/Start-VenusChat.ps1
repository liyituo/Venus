﻿param(
    [switch]$InstallShortcut,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$pythonwExe = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
$guiModule = Join-Path $repoRoot 'src\venuschat_v1\__main__.py'
$probeFile = Join-Path $PSScriptRoot 'probe_venus_backend.py'
$launcherScript = Join-Path $PSScriptRoot 'Start-VenusChat.ps1'
$iconFile = Join-Path $repoRoot 'assets\venuschat.ico'
$markerFile = Join-Path $repoRoot '.venus\venuschat-gui.json'
$logDir = Join-Path $repoRoot '.venus\launcher'

function Install-VenusShortcut {
    if (-not (Test-Path -LiteralPath $iconFile)) { throw "找不到图标：$iconFile" }
    $desktop = [Environment]::GetFolderPath('DesktopDirectory')
    if (-not $desktop) { throw '无法定位当前用户的桌面。' }
    $link = Join-Path $desktop 'VenusChat.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcherScript`""
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.IconLocation = "$iconFile,0"
    $shortcut.Description = '启动 VenusChat 桌面客户端'
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
    } catch { return $null }
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


try {
    if (-not (Test-Path -LiteralPath $guiModule) -or
        -not (Test-Path -LiteralPath $probeFile)) {
        throw 'VenusChat 源文件不完整；请确认从项目的 scripts 目录运行。'
    }
    if ($InstallShortcut) { Install-VenusShortcut }
    if ($NoLaunch) { exit 0 }

    Set-Location -LiteralPath $repoRoot
    if (-not (Test-Path -LiteralPath $pythonExe) -or
        -not (Test-Path -LiteralPath $pythonwExe)) {
        Write-Host '创建桌面客户端 Python 虚拟环境…'
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

    $existingGui = Get-GuiProcess
    if ($null -ne $existingGui) {
        Show-GuiProcess $existingGui
        Write-Host 'VenusChat 已在运行，已尝试切换到现有窗口。'
        exit 0
    }

    # The GUI probes its selected local/remote connection, including TLS and password.
    Write-Host '打开 VenusChat…'
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
            Write-Host 'VenusChat 已启动；可在设置中连接已配置的后端。'
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
