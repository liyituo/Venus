@echo off
title PC Agent Daemon - Stop
cd /d "%~dp0"

rem ============================================================
rem  Stop the background Daemon (find process by port 8000).
rem  This also stops a Daemon that was auto-launched by the GUI.
rem ============================================================

set found=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    set found=1
    taskkill /F /PID %%p >nul 2>&1
)

if "%found%"=="0" (
    echo No Daemon is listening on port 8000.
) else (
    echo Daemon stopped.
)
pause
