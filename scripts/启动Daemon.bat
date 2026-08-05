@echo off
title PC Agent Daemon - Background Start
cd /d "%~dp0"

rem ============================================================
rem  Start Daemon in background (no window, log -> daemon.log)
rem  NOTE: usually not needed - the GUI auto-starts the Daemon.
rem  Use this only for running Daemon standalone (Web console).
rem ============================================================

if not exist ".venv\Scripts\pythonw.exe" (
    echo .venv not found. Run "一键启动控制台.bat" first to initialize.
    pause
    exit /b 1
)

".venv\Scripts\pythonw.exe" app.py --port 8000 > daemon.log 2>&1
echo Daemon started in background (log: daemon.log).
exit /b 0
