<#
.SYNOPSIS
    Refresh the daily Kite access token.

.DESCRIPTION
    Kite access tokens expire every day at roughly 06:00 IST, so run this once
    per trading day before starting the bot. It prints the Kite login URL, asks
    for the request_token from the redirect, and writes KITE_ACCESS_TOKEN back
    into .env (everything else in the file is preserved).

.PARAMETER EnvFile
    Environment file to update. Default: .env next to the repository root.

.EXAMPLE
    .\scripts\windows\login.ps1
#>
[CmdletBinding()]
param(
    [string]$EnvFile = '.env'
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$IsWindowsHost = ($PSVersionTable.PSEdition -eq 'Desktop') -or ($env:OS -eq 'Windows_NT')
$VenvDir = [IO.Path]::Combine($RepoRoot, '.venv')
$VenvPython = if ($IsWindowsHost) {
    [IO.Path]::Combine($VenvDir, 'Scripts', 'python.exe')
} else {
    [IO.Path]::Combine($VenvDir, 'bin', 'python')
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host 'No virtual environment found. Run .\scripts\windows\setup.ps1 first.' -ForegroundColor Red
    exit 1
}

Push-Location $RepoRoot
try {
    & $VenvPython -m kite_alerts.cli.main --env-file $EnvFile login
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -eq 0) {
    Write-Host 'Token refreshed. You can now start .\scripts\windows\run_bot.ps1'
} else {
    Write-Host "Login failed with exit code $code. The message above says why." -ForegroundColor Red
}
exit $code
