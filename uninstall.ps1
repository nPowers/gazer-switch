# Removes the Gazer Startup shortcut and .venv. Asks before deleting config.json.
# ASCII only. Windows PowerShell 5.1 or newer. Run uninstall.bat.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

Write-Host "Gazer uninstall"
$startup = [Environment]::GetFolderPath("Startup")
$shortcut = Join-Path $startup "Gazer.lnk"
$legacy = Join-Path $startup "eKL gaze switch.lnk"

if (Test-Path -LiteralPath $shortcut) {
    Remove-Item -LiteralPath $shortcut -Force
    Write-Host "Removed Startup shortcut: $shortcut"
} else {
    Write-Host "No Gazer Startup shortcut found."
}
if (Test-Path -LiteralPath $legacy) {
    Write-Host "Left the existing 'eKL gaze switch' Startup shortcut in place."
}

$venv = Join-Path $root ".venv"
if (Test-Path -LiteralPath $venv) {
    Remove-Item -LiteralPath $venv -Recurse -Force
    Write-Host "Removed .venv"
} else {
    Write-Host "No .venv to remove."
}

$config = Join-Path $root "config.json"
if (Test-Path -LiteralPath $config) {
    $answer = Read-Host "Delete config.json? This removes saved calibration. [y/N]"
    if ($answer -eq "y" -or $answer -eq "Y") {
        Remove-Item -LiteralPath $config -Force
        Write-Host "Deleted config.json"
    } else {
        Write-Host "Kept config.json"
    }
} else {
    Write-Host "No config.json to delete."
}

Write-Host "Uninstall done. Program files are still in this folder. Delete the folder to remove them."
exit 0
