# Build the Astro Rig Monitor installer for Windows (x64). Runs on Windows (GitHub Actions does it:
# .github/workflows/windows-app.yml); needs Inno Setup 6, which it installs with Chocolatey if missing.
#
#   powershell -ExecutionPolicy Bypass -File windows\build.ps1
#
# Result: windows\build\Astro-Rig-Monitor-Setup.exe
param([string]$Build = "")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Root  = Split-Path -Parent $PSScriptRoot
$Out   = Join-Path $PSScriptRoot "build"
$Stage = Join-Path $Out "stage"
$Cache = Join-Path $env:LOCALAPPDATA "astro-rig-monitor-build"

# Python from python-build-standalone (astral-sh), pinned by checksum: the same release as the Mac app.
$PbsTag = "20261110"
$PbsPy  = "3.12.16"
$PbsSha = "e23fe5b16926de1d81661b1a97352e89fb167549c55a6825796ccd0f32ec7f1b"
$PbsName = "cpython-$PbsPy+$PbsTag-x86_64-pc-windows-msvc-install_only.tar.gz"

if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
New-Item -ItemType Directory -Force -Path $Stage, $Cache | Out-Null

Write-Host "Python $PbsPy"
$Tarball = Join-Path $Cache $PbsName
if (-not (Test-Path $Tarball)) {
    $Url = "https://github.com/astral-sh/python-build-standalone/releases/download/$PbsTag/" + $PbsName.Replace("+", "%2B")
    Invoke-WebRequest -Uri $Url -OutFile "$Tarball.part"
    Move-Item "$Tarball.part" $Tarball
}
if ((Get-FileHash -Algorithm SHA256 $Tarball).Hash.ToLower() -ne $PbsSha) {
    Remove-Item $Tarball
    throw "checksum mismatch: $PbsName"
}
tar -xzf $Tarball -C $Stage                 # gives $Stage\python
$Py = Join-Path $Stage "python\python.exe"

Write-Host "Packages"
& $Py -m pip install --quiet --disable-pip-version-check --no-warn-script-location `
    -r (Join-Path $Root "requirements.txt") -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip failed" }

Write-Host "The monitor and the tray icon"
$App = Join-Path $Stage "app"
New-Item -ItemType Directory -Force -Path $App | Out-Null
Copy-Item -Recurse (Join-Path $Root "arm") (Join-Path $App "arm")
Copy-Item -Recurse (Join-Path $PSScriptRoot "armwin") (Join-Path $App "armwin")
Get-ChildItem -Path $App -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
# Python finds app\ through a .pth file (a scheduled task can't set PYTHONPATH).
Set-Content -Path (Join-Path $Stage "python\Lib\site-packages\astro-rig-monitor.pth") -Value "..\..\..\app" -Encoding ascii
& $Py -m compileall -q $App | Out-Null

$Version = (Get-Content (Join-Path $PSScriptRoot "VERSION")).Trim()
if (-not $Build) { $Build = (git -C $Root rev-list --count HEAD).Trim() }
Set-Content -Path (Join-Path $Stage "VERSION") -Value $Version -Encoding ascii
Set-Content -Path (Join-Path $Stage "BUILD") -Value $Build -Encoding ascii

# The icon, from the Mac app's (the same picture).
& $Py -c "import sys; from PIL import Image; Image.open(sys.argv[1]).save(sys.argv[2], sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])" `
    (Join-Path $Root "mac\Resources\AppIcon.icns") (Join-Path $Stage "icon.ico")
if ($LASTEXITCODE -ne 0) { throw "icon failed" }

# Check it works before packing it.
& $Py -s -B -c "import arm, armwin.tray, armwin.launcher, armwin.task, pystray, qrcode, PIL, paho.mqtt, certifi; print('imports OK, monitor', arm.__version__)"
if ($LASTEXITCODE -ne 0) { throw "the bundled Python can't import everything" }

Write-Host "Installer"
$Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $Iscc)) {
    choco install innosetup -y --no-progress | Out-Null
}
& $Iscc /Q "/DAppVersion=$Version" "/DStage=$Stage" "/O$Out" (Join-Path $PSScriptRoot "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
$Exe = Join-Path $Out "Astro-Rig-Monitor-Setup.exe"
$Hash = (Get-FileHash -Algorithm SHA256 $Exe).Hash.ToLower()
Write-Host "Built $Exe ($([math]::Round((Get-Item $Exe).Length / 1MB)) MB), version $Version build $Build, SHA-256 $Hash"
