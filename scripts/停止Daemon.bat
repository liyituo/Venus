@echo off
title Venus Daemon - Stop
cd /d "%~dp0"

rem ============================================================
rem  Stop the background Daemon via PID metadata JSON
rem  (app.py --pid-file). No WMIC dependency; identity is verified
rem  with PowerShell (Get-CimInstance): PID exists + command line
rem  contains app.py + process creation time matches the metadata.
rem  Never scans port 8000 and kills whatever holds it.
rem  On verification/stop failure the PID file is NOT deleted and
rem  the script does NOT report success.
rem ============================================================

set "ROOT=%~dp0.."
set "PIDFILE=%ROOT%\.venus\daemon.pid"

if not exist "%PIDFILE%" (
    echo 未找到 PID metadata 文件（%PIDFILE%）。
    echo Daemon 可能未通过 scripts\启动Daemon.bat 启动，或已被 GUI 自动拉起。
    echo 本脚本不会扫描端口 8000 盲目杀进程（避免误杀无关程序）。
    pause
    exit /b 1
)

rem ---- read PID (JSON metadata first, fallback to legacy plain PID) ----
set "PID="
for /f "usebackq delims=" %%L in (`powershell -NoProfile -Command "$d=Get-Content -Raw -LiteralPath '%PIDFILE%' | ConvertFrom-Json -ErrorAction SilentlyContinue; if ($d.pid) { $d.pid } else { (Get-Content -LiteralPath '%PIDFILE%' -TotalCount 1).Trim() }"`) do set "PID=%%L"

if "%PID%"=="" (
    echo [错误] PID metadata 文件为空或无法解析，已忽略。
    pause
    exit /b 1
)

rem ---- verify: PID exists, is python, command line contains app.py,
rem       creation time matches metadata (within 120s) ----
set "VERIFY="
for /f "usebackq delims=" %%L in (`powershell -NoProfile -Command "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=%PID%\" -ErrorAction SilentlyContinue; if (-not $p) { 'MISSING'; exit }; if ($p.Name -notlike '*python*') { 'NOT_PYTHON'; exit }; if ($p.CommandLine -notlike '*app.py*') { 'NOT_APP'; exit }; $meta=Get-Content -Raw -LiteralPath '%PIDFILE%' | ConvertFrom-Json -ErrorAction SilentlyContinue; if ($meta.started) { $t=[datetime]::ParseExact($meta.started,'yyyy-MM-dd HH:mm:ss',$null); $ct=[Management.ManagementDateTimeConverter]::ToDateTime($p.CreationDate); if ([math]::Abs(($ct-$t).TotalSeconds) -gt 120) { 'TIME_MISMATCH'; exit } }; 'OK'"`) do set "VERIFY=%%L"

if not "%VERIFY%"=="OK" (
    echo [错误] 进程身份验证失败（%VERIFY%）：PID %PID% 可能已退出、被复用或不属于本 Daemon。
    echo        已保留 PID metadata 文件，未执行任何终止操作。
    pause
    exit /b 1
)

rem ---- graceful shutdown first: POST /api/v1/stop (no token case) ----
powershell -NoProfile -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/v1/stop' -Method Post -TimeoutSec 5 | Out-Null } catch { }" >nul 2>&1

rem ---- wait up to 6s for clean exit ----
set "STILL="
for /l %%i in (1,1,12) do (
    tasklist /FI "PID eq %PID%" /FO CSV 2>nul | findstr /I "python" >nul 2>&1 || goto :exited
    timeout /t 1 /nobreak >nul 2>&1
)
set "STILL=1"

if defined STILL (
    rem ---- controlled PowerShell Stop-Process fallback ----
    powershell -NoProfile -Command "Stop-Process -Id %PID% -Force -ErrorAction SilentlyContinue" >nul 2>&1
    timeout /t 2 /nobreak >nul 2>&1
    tasklist /FI "PID eq %PID%" /FO CSV 2>nul | findstr /I "python" >nul 2>&1
    if not errorlevel 1 (
        echo [错误] 无法终止 PID %PID%（仍在运行）。
        echo        已保留 PID metadata 文件。
        pause
        exit /b 1
    )
)

:exited
del "%PIDFILE%" >nul 2>&1
echo Daemon（PID %PID%）已停止。
pause
exit /b 0
