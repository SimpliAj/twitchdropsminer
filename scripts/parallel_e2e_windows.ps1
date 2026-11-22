# CI: "Add parallel account" end to end on a copy of the built app.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$work = Join-Path $env:RUNNER_TEMP "tdm-parallel-e2e"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $work | Out-Null
Copy-Item dist\TwitchDropsMiner "$work\app" -Recurse
$exe = "$work\app\TwitchDropsMiner.exe"

$env:TDM_DATA_DIR = "$work\data"
$env:TDM_PORT = "18082"
function Healthy($port) { try { (Invoke-WebRequest "http://127.0.0.1:$port/healthz" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { $false } }
function WaitHealthy($port) { foreach ($i in 1..40) { if (Healthy $port) { return }; Start-Sleep 2 }; throw "nothing answers on port $port" }
function StopAll { Get-Process TwitchDropsMiner -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 3 }

Start-Process -FilePath $exe -ArgumentList "--no-tray", "--no-browser"
WaitHealthy 18082
# first-run password setup would otherwise redirect every API call
try { Invoke-WebRequest -Method Post "http://127.0.0.1:18082/__setup_post" -Body @{ action = "skip" } -UseBasicParsing -MaximumRedirection 0 | Out-Null } catch {}
$r = Invoke-RestMethod -Method Post "http://127.0.0.1:18082/api/instances/spawn" -ContentType "application/json" -Body "{}"
$child = $r.instances | Where-Object { $_.spawned } | Select-Object -First 1
Write-Host "spawn: $($child | ConvertTo-Json -Compress)"
if (-not $child) { StopAll; throw "spawn did not register a copy" }
$port = $child.port
WaitHealthy $port
$list = Invoke-RestMethod "http://127.0.0.1:18082/api/instances"
if (-not ($list.instances | Where-Object { $_.port -eq $port -and $_.spawned })) { StopAll; throw "instances list misses the new copy" }
if (-not (Test-Path $child.data_dir)) { StopAll; throw "child data dir missing: $($child.data_dir)" }
Write-Host "child answers on port ${port}, data in $($child.data_dir)"

StopAll
if ((Healthy 18082) -or (Healthy $port)) { throw "apps still running after stop" }
Start-Process -FilePath $exe -ArgumentList "--no-tray", "--no-browser"
WaitHealthy 18082
WaitHealthy $port
Write-Host "child came back with the app"
Invoke-RestMethod -Method Delete "http://127.0.0.1:18082/api/instances/$($child.n)" | Out-Null
foreach ($i in 1..20) { if (-not (Healthy $port)) { break }; Start-Sleep 1 }
if (Healthy $port) { StopAll; throw "removed copy is still running" }
if (-not (Healthy 18082)) { throw "parent died with the removed copy" }
Write-Host "removed copy stopped, parent still up"
StopAll
Write-Host "Parallel accounts end-to-end OK"
