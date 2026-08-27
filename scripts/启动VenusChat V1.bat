@echo off
setlocal
title VenusChat V1

set "VENUSCHAT_ROOT=%~dp0.."
pushd "%VENUSCHAT_ROOT%\src"

if exist "%VENUSCHAT_ROOT%\.venv\Scripts\pythonw.exe" (
    start "" "%VENUSCHAT_ROOT%\.venv\Scripts\pythonw.exe" -m venuschat_v1
    goto :done
)

where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw -m venuschat_v1
    goto :done
)

start "" python -m venuschat_v1

:done
popd
endlocal
exit /b 0

