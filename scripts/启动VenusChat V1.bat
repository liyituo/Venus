@echo off
setlocal
chcp 65001 >nul
title VenusChat Starter
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-VenusChat.ps1" %*
set "VENUS_EXIT=%ERRORLEVEL%"
if not "%VENUS_EXIT%"=="0" pause
exit /b %VENUS_EXIT%
