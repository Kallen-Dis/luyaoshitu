@echo off
setlocal
chcp 65001 >nul
title Luyaoshitu - Service Manager
if "%~1"=="" (
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" -Interactive
) else (
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
)
set "START_EXIT=%ERRORLEVEL%"
if "%~1"=="" if not "%START_EXIT%"=="0" (
  echo.
  pause
)
exit /b %START_EXIT%
