@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

if not "%~4"=="" goto usage

set "TIANDITU_BUNDLE="
if not "%~1"=="" set "TIANDITU_BUNDLE=%~f1"
if defined TIANDITU_BUNDLE if not exist "%TIANDITU_BUNDLE%" (
  echo [错误] 天地图数据包不存在："%TIANDITU_BUNDLE%"
  exit /b 2
)
if defined TIANDITU_BUNDLE if not exist "%~dp1tianditu_250k_batches.csv" (
  echo [错误] 天地图数据包同目录缺少 tianditu_250k_batches.csv。
  exit /b 2
)

set "SERVER_PORT=8000"
if not "%~2"=="" set "SERVER_PORT=%~2"
set "WATCH_ROOT="
if not "%~3"=="" set "WATCH_ROOT=%~f3"
if defined WATCH_ROOT if not exist "%WATCH_ROOT%\." (
  echo [错误] 监控根目录不存在或不是目录："%WATCH_ROOT%"
  exit /b 2
)

if defined ZSL_PYTHON (
  if not exist "%ZSL_PYTHON%" (
    echo [错误] ZSL_PYTHON 指定的 Python 不存在："%ZSL_PYTHON%"
    exit /b 2
  )
  for %%I in ("%ZSL_PYTHON%") do set "ZSL_PYTHON=%%~fI"
  goto runtime_ready
)

call "%~dp0deploy\windows\prepare_runtime.bat"
set "PREPARE_CODE=%ERRORLEVEL%"
if "%PREPARE_CODE%"=="0" goto runtime_ready
if not "%PREPARE_CODE%"=="3" exit /b %PREPARE_CODE%

set "CONDA_CMD="
if defined CONDA_EXE if exist "%CONDA_EXE%" set "CONDA_CMD=%CONDA_EXE%"
if not defined CONDA_CMD where conda >nul 2>nul && set "CONDA_CMD=conda"
if not defined CONDA_CMD if exist "%USERPROFILE%\miniforge3\condabin\conda.bat" set "CONDA_CMD=%USERPROFILE%\miniforge3\condabin\conda.bat"
if not defined CONDA_CMD if exist "%USERPROFILE%\miniconda3\condabin\conda.bat" set "CONDA_CMD=%USERPROFILE%\miniconda3\condabin\conda.bat"
if not defined CONDA_CMD if exist "%LOCALAPPDATA%\miniforge3\condabin\conda.bat" set "CONDA_CMD=%LOCALAPPDATA%\miniforge3\condabin\conda.bat"
if not defined CONDA_CMD (
  echo [错误] 包内 Python 不存在，开发环境中也未找到 conda。
  exit /b 2
)

:runtime_ready
set "ADMIN_DIR=%~dp0data\admin_geojson"
if not exist "%ADMIN_DIR%\中国_省.geojson" set "ADMIN_DIR=%~dp0deploy\windows\data\admin_geojson"
if not exist "%ADMIN_DIR%\中国_省.geojson" set "ADMIN_DIR="
set "TIANDITU_ARGS="
if defined TIANDITU_BUNDLE set "TIANDITU_ARGS=--tianditu-bundle "%TIANDITU_BUNDLE%""
set "ADMIN_ARGS="
if defined ADMIN_DIR set "ADMIN_ARGS=--admin-boundaries-dir "%ADMIN_DIR%""
set "WATCH_ARGS="
if defined WATCH_ROOT set "WATCH_ARGS=--watch-root "%WATCH_ROOT%""
set "BROWSER_ARGS="
if /I "%ZSL_NO_BROWSER%"=="1" set "BROWSER_ARGS=--no-browser"

set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "MPLBACKEND=Agg"
set "MPLCONFIGDIR=%~dp0workspace\runtime-cache\matplotlib"
set "NUMBA_CACHE_DIR=%~dp0workspace\runtime-cache\numba"
if not exist "%MPLCONFIGDIR%\." mkdir "%MPLCONFIGDIR%"
if not exist "%NUMBA_CACHE_DIR%\." mkdir "%NUMBA_CACHE_DIR%"

pushd "%~dp0" || exit /b 2
if defined ZSL_PYTHON (
  "%ZSL_PYTHON%" local_archive_server.py --port "%SERVER_PORT%" %TIANDITU_ARGS% %ADMIN_ARGS% %WATCH_ARGS% %BROWSER_ARGS%
) else (
  call "%CONDA_CMD%" run --no-capture-output -n zsl-windows python local_archive_server.py --port "%SERVER_PORT%" %TIANDITU_ARGS% %ADMIN_ARGS% %WATCH_ARGS% %BROWSER_ARGS%
)
set "RUN_CODE=%ERRORLEVEL%"
popd
exit /b %RUN_CODE%

:usage
echo 用法：%~nx0 ["可选天地图图幅.zip"] [端口] ["监控根目录"]
echo 离线包直接双击 START_ZHEN_SHILAN.bat；已内置行政区地图，无需提供图幅 ZIP。
exit /b 2
