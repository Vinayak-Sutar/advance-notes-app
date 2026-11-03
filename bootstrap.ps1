# Mnemo bootstrap (Windows). Everything installs inside this folder.
#   powershell -ExecutionPolicy Bypass -File bootstrap.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Error "Python 3.10+ is required and was not found on PATH."; exit 1 }

$ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "Python $ver at $($py.Source)"
if ([version]$ver -lt [version]"3.10") { Write-Error "Python 3.10+ required, found $ver"; exit 1 }

Write-Host ""
Write-Host "Working offline is fine -- the app runs with no models at all."
Write-Host "This downloads the local model stack (~5 GB) into .\vendor\ for better quality."
Write-Host ""

& python -m mnemo.setup @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& python -m mnemo.doctor
& python -m mnemo status
