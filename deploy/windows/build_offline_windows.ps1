[CmdletBinding()]
param(
    [string]$Conda,
    [string]$OutputDirectory,
    [string]$MapSource,
    [switch]$IncludeDemoPackages,
    [switch]$KeepBuildDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-LastExitCode([string]$Operation) {
    if ($LASTEXITCODE -ne 0) { throw "$Operation 失败，退出码 $LASTEXITCODE" }
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Convert-WindowsScriptLineEndings([string]$Root) {
    foreach ($File in Get-ChildItem -LiteralPath $Root -File -Recurse) {
        if ($File.Extension -notin ".bat", ".cmd") { continue }
        $Text = [IO.File]::ReadAllText($File.FullName)
        $Text = $Text.Replace("`r`n", "`n").Replace("`r", "`n").Replace("`n", "`r`n")
        Write-Utf8NoBom $File.FullName $Text
    }
}

function Resolve-CondaCommand([string]$Requested) {
    $Candidates = @(
        $Requested,
        $env:CONDA_EXE,
        (Join-Path $env:USERPROFILE "miniforge3\condabin\conda.bat"),
        (Join-Path $env:USERPROFILE "miniconda3\condabin\conda.bat"),
        (Join-Path $env:LOCALAPPDATA "miniforge3\condabin\conda.bat"),
        (Join-Path $env:LOCALAPPDATA "miniconda3\condabin\conda.bat")
    ) | Where-Object { $_ }
    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate -PathType Leaf) { return (Resolve-Path $Candidate).Path }
        $Command = Get-Command $Candidate -ErrorAction SilentlyContinue
        if ($Command) { return $Command.Source }
    }
    throw "未找到 conda。构建机需安装 64 位 Miniforge；最终用户不需要安装。"
}

function Copy-RelativeFile([string]$Relative) {
    $Source = Join-Path $ProjectRoot ($Relative -replace "/", "\")
    if (-not (Test-Path $Source -PathType Leaf)) { throw "源文件不存在: $Relative" }
    $Destination = Join-Path $Stage ($Relative -replace "/", "\")
    New-Item -ItemType Directory -Path (Split-Path $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Copy-Tree([string]$Source, [string]$Destination) {
    if (-not (Test-Path $Source -PathType Container)) { throw "源目录不存在: $Source" }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Copy-Item -Path (Join-Path $Source "*") -Destination $Destination -Recurse -Force
}

function Copy-DemoVisuals([string]$Source, [string]$Destination) {
    $SourcePath = (Resolve-Path $Source).Path.TrimEnd("\")
    foreach ($File in Get-ChildItem -LiteralPath $SourcePath -File -Recurse) {
        $Relative = $File.FullName.Substring($SourcePath.Length).TrimStart("\")
        $Parts = $Relative -split "\\"
        if ($Parts -contains "IF_folder") { continue }
        if (-not $IncludeDemoPackages -and $Parts -contains "packages") { continue }
        if (-not $IncludeDemoPackages -and $File.Extension -in ".dat", ".zip") { continue }
        $Target = Join-Path $Destination $Relative
        New-Item -ItemType Directory -Path (Split-Path $Target) -Force | Out-Null
        Copy-Item -LiteralPath $File.FullName -Destination $Target -Force
    }
}

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "可移植 Windows Python 必须在 Windows x64 构建机上组装。macOS 可运行 verify_windows_runtime.py 做定义验证。"
}
if (-not [System.Environment]::Is64BitOperatingSystem) { throw "只支持 Windows x64。" }

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$OutputDirectory = if ($OutputDirectory) { $OutputDirectory } else { Join-Path $ProjectRoot "dist" }
$MapSource = if ($MapSource) { $MapSource } else { Join-Path $PSScriptRoot "data\admin_geojson" }
$CondaCommand = Resolve-CondaCommand $Conda
$BuildRoot = Join-Path $OutputDirectory ".windows-build"
$EnvironmentPath = Join-Path $BuildRoot "environment"
$RuntimeArchive = Join-Path $BuildRoot "runtime.zip"
$Stage = Join-Path $BuildRoot "震时澜-Windows-x64"
$Package = Join-Path $OutputDirectory "震时澜-Windows-x64-offline-r2.zip"
$PackageHash = "$Package.sha256"

$RequiredMaps = @("中国_省.geojson", "中国_市.geojson", "中国_县.geojson")
foreach ($Filename in $RequiredMaps) {
    if (-not (Test-Path (Join-Path $MapSource $Filename) -PathType Leaf)) {
        throw "地图资源缺失: $(Join-Path $MapSource $Filename)"
    }
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
if (Test-Path $BuildRoot) { Remove-Item -LiteralPath $BuildRoot -Recurse -Force }
New-Item -ItemType Directory -Path $BuildRoot, $Stage -Force | Out-Null

Write-Host "[1/7] 创建固定版本 Windows Python 环境..." -ForegroundColor Cyan
& $CondaCommand env create --prefix $EnvironmentPath --file (Join-Path $ProjectRoot "environment-windows.yml") --yes
Assert-LastExitCode "创建 Python 环境"

Write-Host "[2/7] 验证依赖、应用入口和地图..." -ForegroundColor Cyan
& (Join-Path $EnvironmentPath "python.exe") (Join-Path $PSScriptRoot "verify_windows_runtime.py") `
    --project-root $ProjectRoot --map-root $MapSource
Assert-LastExitCode "Windows 运行时验证"

& $CondaCommand list --prefix $EnvironmentPath --explicit | Set-Content `
    -LiteralPath (Join-Path $BuildRoot "conda-explicit-win-64.txt") -Encoding UTF8
Assert-LastExitCode "导出 conda 依赖清单"
& (Join-Path $EnvironmentPath "python.exe") -m pip freeze --all | Set-Content `
    -LiteralPath (Join-Path $BuildRoot "pip-freeze.txt") -Encoding UTF8
Assert-LastExitCode "导出 pip 依赖清单"

Write-Host "[3/7] 打包可迁移 Python 运行时..." -ForegroundColor Cyan
& (Join-Path $EnvironmentPath "Scripts\conda-pack.exe") --prefix $EnvironmentPath `
    --output $RuntimeArchive --format zip --force
Assert-LastExitCode "打包 Python 运行时"
Expand-Archive -LiteralPath $RuntimeArchive -DestinationPath (Join-Path $Stage ".runtime") -Force

Write-Host "[4/7] 复制程序与 K-NET 可视化演示..." -ForegroundColor Cyan
@(
    "local_archive_server.py",
    "process_events_windows.py",
    "csmnc_reader.py",
    "tianditu_250k.py",
    "event_overrides.example.json",
    "start_windows.bat",
    "run_windows.bat",
    "scan_windows.bat",
    "START_ZHEN_SHILAN.bat",
    "PROCESS_DATA_FOLDER.bat",
    "WINDOWS_README.md",
    "environment-windows.yml",
    "frontend/index.html",
    "frontend/app.js",
    "frontend/data.js",
    "frontend/styles.css",
    "frontend/artifact-layout.js",
    "frontend/attenuation-bins.js",
    "frontend/target-config.js",
    "frontend/demo/build_demo.py",
    "frontend/demo/knet-demo.json",
    "frontend/demo/japan-prefectures.geojson",
    "frontend/demo/world-land.geojson"
) | ForEach-Object { Copy-RelativeFile $_ }
Copy-Tree (Join-Path $ProjectRoot "frontend\assets") (Join-Path $Stage "frontend\assets")
Copy-Tree (Join-Path $ProjectRoot "frontend\vendor") (Join-Path $Stage "frontend\vendor")
Copy-Tree (Join-Path $ProjectRoot "frontend\demo\site_data") (Join-Path $Stage "frontend\demo\site_data")
Copy-DemoVisuals (Join-Path $ProjectRoot "frontend\demo\events") (Join-Path $Stage "frontend\demo\events")

if (-not $IncludeDemoPackages) {
    foreach ($EventJson in Get-ChildItem (Join-Path $Stage "frontend\demo\events") -Filter event.json -File -Recurse) {
        $Payload = Get-Content -LiteralPath $EventJson.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        $Payload.resultPackage = $null
        Write-Utf8NoBom $EventJson.FullName (($Payload | ConvertTo-Json -Depth 100 -Compress) + "`n")
    }
}

$BatchRoot = Join-Path $Stage "frontend\demo\batch"
New-Item -ItemType Directory -Path (Join-Path $BatchRoot "events"), (Join-Path $BatchRoot "packages") -Force | Out-Null
$EmptyCatalog = @{
    schemaVersion = 1
    source = "Windows offline deployment"
    generatedAt = [DateTime]::UtcNow.ToString("o")
    defaultEventId = $null
    defaultBoundaryUrl = "./demo/world-land.geojson"
    eventCount = 0
    events = @()
} | ConvertTo-Json -Depth 4
Write-Utf8NoBom (Join-Path $BatchRoot "catalog.json") ($EmptyCatalog + "`n")

Write-Host "[5/7] 内置行政区地图和部署校验工具..." -ForegroundColor Cyan
Copy-Tree $MapSource (Join-Path $Stage "data\admin_geojson")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "data\MAP_RESOURCE_MANIFEST.json") -Destination (Join-Path $Stage "data\MAP_RESOURCE_MANIFEST.json")
New-Item -ItemType Directory -Path (Join-Path $Stage "workspace\imports"), (Join-Path $Stage "workspace\runtime-cache") -Force | Out-Null
@(
    "deploy/windows/prepare_runtime.bat",
    "deploy/windows/VERIFY_PACKAGE.bat",
    "deploy/windows/verify_bundle.ps1",
    "deploy/windows/verify_windows_runtime.py",
    "deploy/windows/requirements-pip.lock.txt"
) | ForEach-Object { Copy-RelativeFile $_ }
Copy-Item -LiteralPath (Join-Path $BuildRoot "conda-explicit-win-64.txt") -Destination (Join-Path $Stage "DEPENDENCIES-CONDA-EXPLICIT.txt")
Copy-Item -LiteralPath (Join-Path $BuildRoot "pip-freeze.txt") -Destination (Join-Path $Stage "DEPENDENCIES-PIP.txt")

$Deployment = @{
    product = "震·时澜"
    platform = "Windows x64"
    deploymentVersion = "2026.07.16-r2"
    python = "3.12.13"
    offlineAfterDownload = $true
    administrativeMapsBundled = $true
    demoPackagesBundled = [bool]$IncludeDemoPackages
    builtAt = [DateTime]::UtcNow.ToString("o")
} | ConvertTo-Json
Write-Utf8NoBom (Join-Path $Stage "DEPLOYMENT.json") ($Deployment + "`n")
Convert-WindowsScriptLineEndings $Stage

Write-Host "[6/7] 生成逐文件 SHA-256 清单..." -ForegroundColor Cyan
$ManifestLines = foreach ($File in Get-ChildItem -LiteralPath $Stage -File -Recurse | Sort-Object FullName) {
    $Relative = $File.FullName.Substring($Stage.Length).TrimStart("\").Replace("\", "/")
    "{0}  {1}" -f (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $Relative
}
Write-Utf8NoBom (Join-Path $Stage "MANIFEST.sha256") (($ManifestLines -join "`n") + "`n")

Write-Host "[7/7] 压缩最终离线部署包..." -ForegroundColor Cyan
if (Test-Path $Package) { Remove-Item -LiteralPath $Package -Force }
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $Package -CompressionLevel Optimal
$Hash = (Get-FileHash -LiteralPath $Package -Algorithm SHA256).Hash.ToLowerInvariant()
"$Hash  $([IO.Path]::GetFileName($Package))" | Set-Content -LiteralPath $PackageHash -Encoding ASCII

if (-not $KeepBuildDirectory) { Remove-Item -LiteralPath $BuildRoot -Recurse -Force }
Write-Host "完成: $Package" -ForegroundColor Green
Write-Host "SHA-256: $Hash" -ForegroundColor Green
