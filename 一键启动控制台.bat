@echo off
title PC Agent Daemon - One-Click Launcher
cd /d "%~dp0"

rem ============================================================
rem  One-Click launcher: double-click this file to open the Chat.
rem  First run: auto-creates .venv and installs dependencies.
rem  The Chat auto-starts / reuses the Daemon.
rem  Use "Open Screen Backend" button inside Chat to open the screen panel.
rem ============================================================

if not exist ".venv\Scripts\pythonw.exe" (
    echo [1/3] First run: creating virtual environment ...
    python -m venv .venv
    if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" -c "import fastapi, pyautogui, uvicorn, PIL" >nul 2>&1
if errorlevel 1 (
    echo [2/3] Installing dependencies - first run, 1-2 min ...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

echo [3/3] Starting Chat ...
start "" ".venv\Scripts\pythonw.exe" chat.py
echo.
echo Chat started - use "Open Screen Backend" button to open the screen panel.
echo This window will close automatically.
exit /b 0

:fail
echo.
echo FAILED - check the error messages above.
pause
exit /b 1
