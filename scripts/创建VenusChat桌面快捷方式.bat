@echo off
setlocal
chcp 65001 >nul
title VenusChat Shortcut
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-VenusChat.ps1" -InstallShortcut -NoLaunch
set "VENUS_EXIT=%ERRORLEVEL%"
pause
exit /b %VENUS_EXIT%
