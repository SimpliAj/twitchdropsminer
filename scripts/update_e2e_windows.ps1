# CI: run the in-app updater end to end on a copy of the built app (zip from dist\, no network).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$work = Join-Path $env:RUNNER_TEMP "tdm-update-e2e"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory "$work\source", "$work\apps" | Out-Null
$zip = Get-ChildItem dist\TwitchDropsMiner-*-windows-x64.zip | Select-Object -First 1
Copy-Item $zip.FullName "$work\source\"
$hash = (Get-FileHash $zip.FullName -Algorithm SHA256).Hash.ToLower()
"$hash  $($zip.Name)" | Set-Content "$work\source\SHA256SUMS-windows.txt" -Encoding ascii
Copy-Item dist\TwitchDropsMiner "$work\apps\TwitchDropsMiner" -Recurse
Set-Content "$work\apps\TwitchDropsMiner\OLD_MARKER" "old"

$env:TDM_DATA_DIR = "$work\data"
$env:TDM_PORT = "18081"
$env:TDM_UPDATE_LOCAL_DIR = "$work\source"
# Like a real start from Explorer: working directory = the app folder, and the dashboard window
# (Edge in app mode) opens. That window used to keep the folder locked so the update was rolled back.
Start-Process -FilePath "$work\apps\TwitchDropsMiner\TwitchDropsMiner.exe" -ArgumentList "--no-tray" -WorkingDirectory "$work\apps\TwitchDropsMiner"
function Healthy { try { (Invoke-WebRequest "http://127.0.0.1:18081/healthz" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { $false } }
foreach ($i in 1..30) { if (Healthy) { break }; Start-Sleep 2 }
if (-not (Healthy)) { throw "copy of the app did not start" }

# first-run password setup would otherwise redirect every API call
try { Invoke-WebRequest -Method Post "http://127.0.0.1:18081/__setup_post" -Body @{ action = "skip" } -UseBasicParsing -MaximumRedirection 0 | Out-Null } catch {}
$r = Invoke-RestMethod -Method Post "http://127.0.0.1:18081/api/self-update"
Write-Host "self-update: $($r | ConvertTo-Json -Compress)"
$ok = $false
foreach ($i in 1..60) {
  Start-Sleep 3
  if (-not (Test-Path "$work\apps\TwitchDropsMiner\OLD_MARKER") -and (Healthy)) { $ok = $true; break }
}
Start-Sleep 12  # the updater script checks the new app stays up for 10 s before cleaning up
$st = try { Invoke-RestMethod "http://127.0.0.1:18081/api/update/status" } catch { $null }
Write-Host "status after update: $($st | ConvertTo-Json -Compress)"
Get-Process TwitchDropsMiner -ErrorAction SilentlyContinue | Stop-Process -Force
if (-not $ok) {
  Get-ChildItem "$work\apps" -Force | Out-Host
  Write-Host "TEMP=$env:TEMP"
  Get-ChildItem $env:TEMP -Filter "tdm-update*" | Out-Host
  Get-Process powershell -ErrorAction SilentlyContinue | Out-Host
  Get-Content "$env:TEMP\tdm-update.log" -ErrorAction SilentlyContinue | Out-Host
  Get-Content "$work\data\logs\startup.log" -ErrorAction SilentlyContinue | Out-Host
  throw "the app was not replaced and restarted"
}
foreach ($leftover in "$work\apps\TwitchDropsMiner.old", "$work\apps\.TwitchDropsMiner-update") {
  if (Test-Path $leftover) { throw "not cleaned up: $leftover" }
}
if (-not (Test-Path "$work\data")) { throw "user data dir vanished" }
Write-Host "Update end-to-end OK"
