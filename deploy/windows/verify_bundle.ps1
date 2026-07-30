[CmdletBinding()]
param([switch]$SkipFullHash)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $Root "runtime\python\python.exe"
if (-not (Test-Path $Python -PathType Leaf)) { $Python = Join-Path $Root ".runtime\python.exe" }
$Manifest = Join-Path $Root "MANIFEST.sha256"
$MapRoot = Join-Path $Root "data\admin_geojson"

if (-not (Test-Path $Python -PathType Leaf)) { throw "缺少包内 Python：runtime\python\python.exe" }
if (-not (Test-Path $Manifest -PathType Leaf)) { throw "缺少文件清单: $Manifest" }

$ScriptCount = 0
foreach ($Script in Get-ChildItem -LiteralPath $Root -File -Recurse) {
    if ($Script.Extension -notin ".bat", ".cmd") { continue }
    $Bytes = [IO.File]::ReadAllBytes($Script.FullName)
    for ($Index = 0; $Index -lt $Bytes.Length; $Index += 1) {
        if ($Bytes[$Index] -eq 10 -and ($Index -eq 0 -or $Bytes[$Index - 1] -ne 13)) {
            throw "Windows 脚本不是 CRLF 换行: $($Script.FullName.Substring($Root.Length + 1))"
        }
        if ($Bytes[$Index] -eq 13 -and ($Index + 1 -ge $Bytes.Length -or $Bytes[$Index + 1] -ne 10)) {
            throw "Windows 脚本包含孤立 CR 字符: $($Script.FullName.Substring($Root.Length + 1))"
        }
    }
    $ScriptCount += 1
}
Write-Host "[通过] $ScriptCount 个 Windows 脚本均为 CRLF 换行。" -ForegroundColor Green

if (-not $SkipFullHash) {
    $Checked = 0
    foreach ($Line in Get-Content -LiteralPath $Manifest -Encoding UTF8) {
        if (-not $Line.Trim()) { continue }
        $Parts = $Line -split "  ", 2
        if ($Parts.Count -ne 2) { throw "清单格式错误: $Line" }
        $File = Join-Path $Root ($Parts[1] -replace "/", "\")
        if (-not (Test-Path $File -PathType Leaf)) { throw "清单文件缺失: $($Parts[1])" }
        $Actual = (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Actual -ne $Parts[0]) { throw "文件校验失败: $($Parts[1])" }
        $Checked += 1
    }
    Write-Host "[通过] $Checked 个文件 SHA-256 校验完成。" -ForegroundColor Green
}

& (Join-Path $PSScriptRoot "prepare_runtime.bat")
if ($LASTEXITCODE -ne 0) { throw "包内 Python 首次离线初始化失败。" }
& $Python (Join-Path $PSScriptRoot "verify_windows_runtime.py") --project-root $Root --map-root $MapRoot
if ($LASTEXITCODE -ne 0) { throw "Python 运行时或资源验证失败。" }
Write-Host "[通过] 震·时澜 Windows 离线包可以运行。" -ForegroundColor Green
