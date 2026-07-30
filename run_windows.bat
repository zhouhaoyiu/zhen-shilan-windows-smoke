@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

if "%~1"=="" goto usage
if not "%~4"=="" goto usage
set "DATA_ROOT=%~f1"
if not exist "%DATA_ROOT%\." (
  echo [错误] 数据总目录不存在："%DATA_ROOT%"
  exit /b 2
)
set "OVERRIDE_FILE="
if not "%~2"=="" set "OVERRIDE_FILE=%~f2"
if defined OVERRIDE_FILE if not exist "%OVERRIDE_FILE%" (
  echo [错误] 元数据覆盖文件不存在："%OVERRIDE_FILE%"
  exit /b 2
)
set "TIANDITU_BUNDLE="
if not "%~3"=="" set "TIANDITU_BUNDLE=%~f3"
if defined TIANDITU_BUNDLE if not exist "%TIANDITU_BUNDLE%" (
  echo [错误] 天地图数据包不存在："%TIANDITU_BUNDLE%"
  exit /b 2
)
if defined TIANDITU_BUNDLE if not exist "%~dp3tianditu_250k_batches.csv" (
  echo [错误] 天地图数据包同目录缺少 tianditu_250k_batches.csv。
  exit /b 2
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
set "OVERRIDE_ARGS="
if defined OVERRIDE_FILE set "OVERRIDE_ARGS=--metadata-overrides "%OVERRIDE_FILE%""
set "TIANDITU_ARGS="
if defined TIANDITU_BUNDLE set "TIANDITU_ARGS=--tianditu-bundle "%TIANDITU_BUNDLE%""
set "ADMIN_ARGS="
if defined ADMIN_DIR set "ADMIN_ARGS=--admin-boundaries-dir "%ADMIN_DIR%""
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "MPLBACKEND=Agg"
set "MPLCONFIGDIR=%~dp0workspace\runtime-cache\matplotlib"
set "NUMBA_CACHE_DIR=%~dp0workspace\runtime-cache\numba"
if not exist "%MPLCONFIGDIR%\." mkdir "%MPLCONFIGDIR%"
if not exist "%NUMBA_CACHE_DIR%\." mkdir "%NUMBA_CACHE_DIR%"

pushd "%~dp0" || exit /b 2
if defined ZSL_PYTHON (
  "%ZSL_PYTHON%" process_events_windows.py --input-root "%DATA_ROOT%" --serve %OVERRIDE_ARGS% %TIANDITU_ARGS% %ADMIN_ARGS%
) else (
  call "%CONDA_CMD%" run --no-capture-output -n zsl-windows python process_events_windows.py --input-root "%DATA_ROOT%" --serve %OVERRIDE_ARGS% %TIANDITU_ARGS% %ADMIN_ARGS%
)
set "RUN_CODE=%ERRORLEVEL%"
popd
exit /b %RUN_CODE%

:usage
echo 用法：%~nx0 "数据总目录" ["event_overrides.json"] ["可选天地图图幅.zip"]
echo 离线包已内置行政区地图，不需要第 3 个参数。
exit /b 2
