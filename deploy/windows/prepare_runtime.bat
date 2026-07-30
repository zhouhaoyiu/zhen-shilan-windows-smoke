@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

set "ZSL_RUNTIME_ROOT="
set "ZSL_PYTHON="
if exist "%~dp0..\..\runtime\python\python.exe" (
  set "ZSL_RUNTIME_ROOT=%~dp0..\..\runtime"
  set "ZSL_PYTHON=%~dp0..\..\runtime\python\python.exe"
  goto runtime_found
)
if exist "%~dp0..\..\.runtime\python.exe" (
  set "ZSL_RUNTIME_ROOT=%~dp0..\..\.runtime"
  set "ZSL_PYTHON=%~dp0..\..\.runtime\python.exe"
  goto runtime_found
)
exit /b 3

:runtime_found
for %%I in ("%ZSL_RUNTIME_ROOT%") do set "ZSL_RUNTIME_ROOT=%%~fI"
for %%I in ("%ZSL_PYTHON%") do set "ZSL_PYTHON=%%~fI"

if exist "%ZSL_RUNTIME_ROOT%\Scripts\conda-unpack.exe" if not exist "%ZSL_RUNTIME_ROOT%\.zsl-relocated" (
  "%ZSL_RUNTIME_ROOT%\Scripts\conda-unpack.exe"
  if errorlevel 1 (
    echo [错误] 包内 Python 路径初始化失败。
    exit /b 2
  )
  type nul > "%ZSL_RUNTIME_ROOT%\.zsl-relocated"
)

set "WHEELHOUSE=%~dp0..\..\wheelhouse"
if not exist "%WHEELHOUSE%\." goto ready
set "WHEEL_LOCK=%WHEELHOUSE%\requirements-windows.lock.txt"
if not exist "%WHEEL_LOCK%" set "WHEEL_LOCK=%WHEELHOUSE%\requirements.lock.txt"
if not exist "%WHEEL_LOCK%" (
  echo [错误] wheelhouse 存在，但缺少 requirements-windows.lock.txt。
  exit /b 2
)

set "READY_FILE=%ZSL_RUNTIME_ROOT%\.zsl-wheelhouse-ready"
set "WHEEL_LOCK_SHA256="
set "WHEEL_LOCK_SHA256_FILE=%TEMP%\zsl-wheel-lock-%RANDOM%-%RANDOM%.sha256"
"%ZSL_PYTHON%" -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "%WHEEL_LOCK%" > "%WHEEL_LOCK_SHA256_FILE%"
if errorlevel 1 (
  if exist "%WHEEL_LOCK_SHA256_FILE%" del "%WHEEL_LOCK_SHA256_FILE%" >nul 2>nul
  echo [错误] 无法计算 wheelhouse 锁文件摘要。
  exit /b 2
)
set /p "WHEEL_LOCK_SHA256="<"%WHEEL_LOCK_SHA256_FILE%"
del "%WHEEL_LOCK_SHA256_FILE%" >nul 2>nul
if not defined WHEEL_LOCK_SHA256 (
  echo [错误] 无法计算 wheelhouse 锁文件摘要。
  exit /b 2
)
set "READY_SHA256="
if exist "%READY_FILE%" set /p "READY_SHA256="<"%READY_FILE%"
if /I "%READY_SHA256%"=="%WHEEL_LOCK_SHA256%" goto ready
echo [首次启动] 正在从包内 wheelhouse 安装固定版本依赖，不访问网络...
"%ZSL_PYTHON%" -m pip --version >nul 2>nul
if errorlevel 1 "%ZSL_PYTHON%" -m ensurepip --upgrade
if errorlevel 1 (
  echo [错误] 包内 Python 无法启用 pip。
  exit /b 2
)
"%ZSL_PYTHON%" -m pip install --no-index --disable-pip-version-check --only-binary=:all: --require-hashes --find-links "%WHEELHOUSE%" -r "%WHEEL_LOCK%"
if errorlevel 1 (
  echo [错误] 离线 Python 依赖安装失败，请运行 VERIFY_PACKAGE.bat 查看明细。
  exit /b 2
)
"%ZSL_PYTHON%" -c "import h5py, matplotlib, numba, numpy, obspy, pandas, pykooh, pyrotd, scipy, shapefile"
if errorlevel 1 (
  echo [错误] 离线依赖导入验证失败。
  exit /b 2
)
>"%READY_FILE%" echo %WHEEL_LOCK_SHA256%

:ready
endlocal & set "ZSL_RUNTIME_ROOT=%ZSL_RUNTIME_ROOT%" & set "ZSL_PYTHON=%ZSL_PYTHON%"
exit /b 0
