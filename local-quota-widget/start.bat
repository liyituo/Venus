@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0..\.venv\Scripts\pythonw.exe" (
  start "quota-widget" /D "%~dp0" "%~dp0..\.venv\Scripts\pythonw.exe" "%~dp0app.py"
) else if exist "%~dp0..\.venv\Scripts\python.exe" (
  start "quota-widget" /D "%~dp0" "%~dp0..\.venv\Scripts\python.exe" "%~dp0app.py"
) else (
  start "quota-widget" /D "%~dp0" pythonw "%~dp0app.py"
)
