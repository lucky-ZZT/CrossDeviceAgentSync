[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repairScript = Join-Path $PSScriptRoot 'repair_archive_incident_20260818.py'
if (-not (Test-Path -LiteralPath $repairScript -PathType Leaf)) {
    throw "Repair script not found: $repairScript"
}

$python = (Get-Command py.exe -ErrorAction Stop).Source
$taskId = '019fcd5a-0060-7602-9c1e-decb68eff0b4'
$codexHome = Join-Path $HOME '.codex'
$escapedPython = $python.Replace("'", "''")
$escapedScript = $repairScript.Replace("'", "''")
$escapedHome = $codexHome.Replace("'", "''")
$command = @"
`$Host.UI.RawUI.WindowTitle = 'CrossDeviceAgentSync Archive Repair'
& '$escapedPython' -3.12 '$escapedScript' --codex-home '$escapedHome' --wait-seconds 1800 --confirm-task-id '$taskId'
Write-Host ''
Write-Host '按 Enter 关闭此修复窗口。'
[void](Read-Host)
"@

Start-Process -FilePath 'powershell.exe' -ArgumentList @(
    '-NoLogo',
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-NoExit',
    '-Command', $command
)

Write-Host '独立修复窗口已启动。请确认窗口显示等待提示后，再从 Codex 菜单正常退出。'
