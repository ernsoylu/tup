# tup installer (Windows).
#
# Installs tup.exe into ~\.tup\bin (tup's home directory) and adds that folder
# to your user PATH so `tup` works directly in any new terminal.
#
# Usage (from an extracted release archive):
#   powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$binary = Join-Path $scriptDir "tup.exe"
if (-not (Test-Path $binary)) {
    Write-Error "tup.exe not found next to install.ps1 — run this from inside the extracted release archive."
}

$installDir = Join-Path $HOME ".tup\bin"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item $binary (Join-Path $installDir "tup.exe") -Force
Write-Host "✅ Installed $installDir\tup.exe"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$installDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$installDir", "User")
    Write-Host "✅ Added $installDir to your user PATH — open a new terminal to use `tup`."
} else {
    Write-Host "✅ $installDir is already on your PATH."
}

Write-Host ""
Write-Host "Run 'tup' to get started (first launch opens the setup wizard),"
Write-Host "or 'tup gui' for the graphical explorer."
