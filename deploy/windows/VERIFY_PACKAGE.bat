@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify_bundle.ps1"
if errorlevel 1 (
  echo [错误] 离线包验证失败。
  pause
  exit /b 1
)
pause
