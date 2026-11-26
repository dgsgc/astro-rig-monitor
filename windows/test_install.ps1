# CI: install the built Windows app silently, run it against the fake rig, update-restart it,
# stop it, uninstall it. Read-only to rigs: the only rig is the fake one on this machine.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
$Exe  = Join-Path $PSScriptRoot "build\Astro-Rig-Monitor-Setup.exe"
$App  = Join-Path $env:LOCALAPPDATA "Programs\Astro Rig Monitor"
$Data = Join-Path $env:LOCALAPPDATA "Astro Rig Monitor"
$Py   = Join-Path $App "python\python.exe"

function Wait-For($Url, $Text) {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
            if ($r.Content -match [regex]::Escape($Text)) { return $r.Content }
        } catch {}
        Start-Sleep 1
    }
    if (Test-Path "$Data\console.log") { Get-Content "$Data\console.log" -Tail 40 }
    throw "no '$Text' at $Url"
}
function App-Processes { Get-Process python, pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$App\*" } }

Write-Host "== install silently"
Start-Process $Exe -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
if (-not (Test-Path (Join-Path $App "python\pythonw.exe"))) { throw "not installed" }
& $Py -s -B -m armwin.tray --check
if ($LASTEXITCODE -ne 0) { throw "tray check failed" }

Write-Host "== fake rig"
$env:PYTHONPATH = $Root
Start-Process $Py -ArgumentList "-s", "$Root\tools\fake_rig.py", "--port", "4411", "--dialect", "phd2", "--loop" -WindowStyle Hidden
Remove-Item Env:PYTHONPATH

Write-Host "== the scheduled task starts the monitor"
& $Py -s -B -m armwin.task install
$page = Wait-For "http://127.0.0.1:8080/setup" "Astro Rig Monitor setup"
$token = [regex]::Match($page, 'const TOKEN = "([^"]+)"').Groups[1].Value

Write-Host "== set up through the page"
$rig = @{ name = "Test rig"; host = "127.0.0.1"; port = 4411; dialect = "phd2"; units = "pixels"; guide_scale = 1.5 }
$body = @{ mode = "new"; web_port = 8080; report = $true; rigs = @($rig) } | ConvertTo-Json -Depth 5
Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:8080/setup/api/save" -Body $body `
    -ContentType "application/json" -Headers @{ "X-Setup-Token" = $token } | Out-Null
Wait-For "http://127.0.0.1:8080/state.json" "test_rig" | Out-Null
& $Py -s -B -m armwin.tray --check

Write-Host "== add a rig: the monitor restarts itself through the launcher"
$page = Wait-For "http://127.0.0.1:8080/setup" "Astro Rig Monitor setup"
$token = [regex]::Match($page, 'const TOKEN = "([^"]+)"').Groups[1].Value
$rig.name = "Second rig"
$body = @{ mode = "add"; rigs = @($rig) } | ConvertTo-Json -Depth 5
Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:8080/setup/api/save" -Body $body `
    -ContentType "application/json" -Headers @{ "X-Setup-Token" = $token } | Out-Null
Wait-For "http://127.0.0.1:8080/state.json" "second_rig" | Out-Null
Get-Content "$Data\console.log" -Tail 5

Write-Host "== a silent update over the running monitor starts it again"
Start-Process $Exe -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
Wait-For "http://127.0.0.1:8080/state.json" "second_rig" | Out-Null

Write-Host "== stopping the task leaves no monitor behind"
& $Py -s -B -m armwin.task stop
Start-Sleep 3
$left = App-Processes | Where-Object { $_.CommandLine -notmatch "fake_rig" }
if ($left) { $left | Format-Table Id, Path, CommandLine; throw "processes left after stopping the task" }

Write-Host "== uninstall"
Start-Process (Join-Path $App "unins000.exe") -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
Start-Sleep 3
if (Test-Path (Join-Path $App "python")) { throw "app folder still there" }
schtasks /Query /TN "Astro Rig Monitor" 2>$null
if ($LASTEXITCODE -eq 0) { throw "task still there" }
if (-not (Test-Path "$Data\config.toml")) { throw "settings were removed" }
Write-Host "all good"
exit 0
