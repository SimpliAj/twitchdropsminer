<#
.SYNOPSIS
  Builds the Windows desktop app: dist\TwitchDropsMiner\TwitchDropsMiner.exe (+ a zip).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".build-venv")) {
    python -m venv .build-venv
}
& ".build-venv\Scripts\python.exe" -m pip install --upgrade pip
& ".build-venv\Scripts\python.exe" -m pip install -r packaging\windows\requirements.txt
& ".build-venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean packaging\windows\tdm.spec

$version = (Select-String -Path src\version.py -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$zip = "dist\TwitchDropsMiner-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "dist\TwitchDropsMiner" -DestinationPath $zip
Write-Host "Built $zip"
