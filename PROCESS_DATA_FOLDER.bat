@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

set "DATA_ROOT=%~1"
if not defined DATA_ROOT set /p "DATA_ROOT=请粘贴数据总目录并按回车："
if not defined DATA_ROOT exit /b 2
call "%~dp0run_windows.bat" "%DATA_ROOT%"
if errorlevel 1 pause
exit /b %ERRORLEVEL%
