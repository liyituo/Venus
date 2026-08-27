@echo off
title Venus - One-Click Launcher
cd /d "%~dp0"

rem ============================================================
rem  One-Click launcher: starts VenusChat V1 desktop UI
rem  and ensures llm_server is reachable.
rem  First run: auto-creates .venv and installs dependencies.
rem ============================================================

if not exist "..\.venv\Scripts\pythonw.exe" (
    echo [1/3] First run: creating virtual environment ...
    python -m venv ..\.venv
    if errorlevel 1 goto :fail
)

"..\.venv\Scripts\python.exe" -c "import fastapi, pyautogui, uvicorn, PIL" >nul 2>&1
if errorlevel 1 (
    echo [2/3] Installing dependencies - first run, 1-2 min ...
    "..\.venv\Scripts\python.exe" -m pip install -r ..\requirements.txt
    if errorlevel 1 goto :fail
)

if not exist "..\src\venuschat_v1\__main__.py" (
    echo VenusChat V1 not found at src\venuschat_v1
    echo Use CLI instead: .venv\Scripts\python src\cli.py
    goto :fail
)

echo [3/3] Starting VenusChat V1 ...
start "" "..\.venv\Scripts\pythonw.exe" -m venuschat_v1
echo.
echo VenusChat V1 started. Ensure llm_server is running on :8001.
echo This window will close automatically.
exit /b 0

:fail
echo.
echo FAILED - check the error messages above.
pause
exit /b 1
