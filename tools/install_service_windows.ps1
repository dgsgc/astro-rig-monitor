# Install or update the Astro Rig Monitor as a Windows background task (Task Scheduler).
#
#   powershell -ExecutionPolicy Bypass -File tools\install_service_windows.ps1           install / update and (re)start
#   ... install_service_windows.ps1 -Config D:\private\config.toml                  use a config.toml kept somewhere else
#   powershell -ExecutionPolicy Bypass -File tools\install_service_windows.ps1 -Remove   stop and remove the task (keeps the folder)
#
# Runs from its own copy in %USERPROFILE%\astro-rig-monitor (or $env:ARM_HOME), starts when you log
# in, restarts if it stops, and keeps running on battery. Run it again after changing config.toml
# or updating the code. Log: %USERPROFILE%\astro-rig-monitor\arm.log
# Needs Python 3.11 or later from python.org (tick "Add python.exe to PATH") or the Microsoft Store.
param([switch]$Remove, [string]$Config)
$ErrorActionPreference = "Stop"

$Src  = Split-Path -Parent $PSScriptRoot
$Dest = if ($env:ARM_HOME) { $env:ARM_HOME } else { Join-Path $env:USERPROFILE "astro-rig-monitor" }
$Task = "Astro Rig Monitor"

if ($Remove) {
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task stopped and removed. $Dest is left in place."
    exit 0
}

if (-not $Config) { $Config = if ($env:ARM_CONFIG) { $env:ARM_CONFIG } else { Join-Path $Src "config.toml" } }
if (-not (Test-Path $Config)) {
    Write-Host "No $Config. Run: py -m arm setup"; exit 1
}
if (Get-Command py -ErrorAction SilentlyContinue) { $PyExe = "py"; $PyArgs = @("-3") } else { $PyExe = "python"; $PyArgs = @() }
& $PyExe @PyArgs -c "import sys; sys.exit(sys.version_info < (3, 11))"
if ($LASTEXITCODE -ne 0) { Write-Host "Python 3.11 or later is needed."; exit 1 }

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$PkgDest = Join-Path $Dest "arm"
if (Test-Path $PkgDest) { Remove-Item -Recurse -Force $PkgDest }
Copy-Item -Recurse (Join-Path $Src "arm") $PkgDest
Get-ChildItem -Path $PkgDest -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Copy-Item (Join-Path $Src "requirements.txt") $Dest -Force
Copy-Item $Config (Join-Path $Dest "config.toml") -Force

$VenvPy = Join-Path $Dest "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { & $PyExe @PyArgs -m venv (Join-Path $Dest "venv") }
& $VenvPy -m pip install -q --disable-pip-version-check -r (Join-Path $Dest "requirements.txt")
Push-Location $Dest
& $VenvPy -c "from arm.config import load; load('config.toml')"
$ok = $LASTEXITCODE -eq 0
Pop-Location
if (-not $ok) { Write-Host "config.toml has a problem (see above)."; exit 1 }

$Pyw = Join-Path $Dest "venv\Scripts\pythonw.exe"        # no console window
$Action   = New-ScheduledTaskAction -Execute $Pyw -Argument "-m arm --config `"$Dest\config.toml`"" -WorkingDirectory $Dest
$Trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew
Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $Task -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Astro Rig Monitor (read-only to the rigs)" -Force | Out-Null
Start-ScheduledTask -TaskName $Task
Write-Host "Astro Rig Monitor installed and started from $Dest. Log: $Dest\arm.log"
