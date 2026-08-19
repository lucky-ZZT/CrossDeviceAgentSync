param(
    [string]$OutputDirectory = "",
    [string]$ApplicationName = "CrossDeviceAgentSync",
    [switch]$AllowUnconfiguredRepository
)

$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $PSScriptRoot
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $SkillRoot)
$BuildRoot = Join-Path $ProjectRoot ".skill-tests\cross-device-agent-sync-build"
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $SkillRoot "assets"
}

$ReleaseChecker = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot "app_release_checker.py")
if (-not $AllowUnconfiguredRepository -and $ReleaseChecker -match 'GITHUB_REPOSITORY\s*=\s*""') {
    throw "Set GITHUB_REPOSITORY in scripts\app_release_checker.py before building a release EXE."
}

New-Item -ItemType Directory -Force $BuildRoot, $OutputDirectory | Out-Null
$EntryPoint = Join-Path $PSScriptRoot "simple_sync_gui.py"
py -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $ApplicationName `
    --distpath $OutputDirectory `
    --workpath (Join-Path $BuildRoot "work") `
    --specpath (Join-Path $BuildRoot "spec") `
    --paths $PSScriptRoot `
    $EntryPoint;
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 构建失败，未生成可用 EXE。"
}

$OutputPath = Join-Path $OutputDirectory "$ApplicationName.exe"
if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) {
    throw "PyInstaller 未生成预期的 EXE：$OutputPath"
}
$Version = if ($ApplicationName -match '^CrossDeviceAgentSync-v(?<version>\d+\.\d+\.\d+)$') {
    $Matches.version
} else {
    $null
}
if ($Version) {
    Get-ChildItem -LiteralPath $OutputDirectory -Filter "CrossDeviceAgentSync*.exe" -File |
        Where-Object { $_.FullName -ne $OutputPath } |
        ForEach-Object {
            $OldPath = $_.FullName
            try {
                Remove-Item -LiteralPath $OldPath -Force -ErrorAction Stop
            }
            catch {
                Write-Warning "旧版本正在使用或无法删除，已保留：$OldPath"
            }
        }
}
$ArtifactHash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToUpperInvariant()
"$ArtifactHash *$(Split-Path -Leaf $OutputPath)" |
    Set-Content -LiteralPath (Join-Path $OutputDirectory "SHA256SUMS.txt") -Encoding ASCII
if (Test-Path -LiteralPath $BuildRoot) {
    try {
        Remove-Item -LiteralPath $BuildRoot -Recurse -Force -ErrorAction Stop
    }
    catch {
        Write-Warning "无法清理本次构建缓存：$BuildRoot"
    }
}

Write-Output $OutputPath

