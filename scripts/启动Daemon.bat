@echo off
title Venus Daemon - Background Start
cd /d "%~dp0"

rem ============================================================
rem  Start Daemon in background (no window, log -> .venus\daemon.err.log)
rem  NOTE: usually not needed - the GUI auto-starts the Daemon.
rem  Use this only for running Daemon standalone (Web console).
rem  Writes PID file (.venus\daemon.pid) so 停止Daemon.bat can
rem  stop only this process (never kills unrelated port-8000 processes).
rem ============================================================

rem ---- locate project root (this script lives in scripts\) ----
set "ROOT=%~dp0.."
set "VENV_PY=%ROOT%\.venv\Scripts\pythonw.exe"
set "APP=%ROOT%\src\app.py"
set "PIDFILE=%ROOT%\.venus\daemon.pid"

rem ---- pre-flight checks ----
if not exist "%VENV_PY%" (
    echo [错误] 未找到虚拟环境：%VENV_PY%
    echo        请先运行 scripts\一键启动控制台.bat 初始化。
    exit /b 1
)
if not exist "%APP%" (
    echo [错误] 未找到 Daemon 入口：%APP%
    exit /b 1
)
"%VENV_PY%" -c "import fastapi, pyautogui, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo [错误] 虚拟环境中缺少依赖（fastapi/pyautogui/uvicorn），请先运行一键启动控制台.bat。
    exit /b 1
)

rem ---- start daemon; the python side writes daemon.pid metadata with its own PID ----
if not exist "%ROOT%\.venus" mkdir "%ROOT%\.venus"

rem stderr/stdout 重定向到 daemon.err.log（该文件真实存在，与提示一致）
start "Venus Daemon" cmd /c ""%VENV_PY%" "%APP%" --port 8000 --pid-file "%PIDFILE%" > "%ROOT%\.venus\daemon.err.log" 2>&1"
if errorlevel 1 (
    echo [错误] Daemon 启动失败（exit code %errorlevel%）。
    exit /b 1
)
echo Daemon 已在后台启动（输出日志：%ROOT%\.venus\daemon.err.log，PID metadata：%PIDFILE%）。
exit /b 0
