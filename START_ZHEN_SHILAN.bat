@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

call "%~dp0start_windows.bat"
if errorlevel 1 pause
exit /b %ERRORLEVEL%
