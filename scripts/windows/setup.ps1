<#
.SYNOPSIS
    Create the virtual environment, install kite-alerts and validate the config.

.DESCRIPTION
    Idempotent: an existing .venv is reused and an existing .env is never
    overwritten, so this is safe to re-run after every update.

    Paths are resolved from this script's location, so it can be run from any
    working directory.

.PARAMETER Python
    Interpreter used to build the venv. Default: the first of py, python,
    python3 found on PATH.

.PARAMETER SkipInstall
    Do not touch the venv or run pip; only create .env when missing and run
    config-check.

.EXAMPLE
    .\scripts\windows\setup.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\windows\setup.ps1
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [switch]$SkipInstall
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

function Get-BasePython {
    param([string]$Requested)

    if ($Requested) {
        $explicit = Get-Command $Requested -ErrorAction SilentlyContinue
        if (-not $explicit) { throw "Python '$Requested' was not found on PATH." }
        return $explicit.Source
    }
    foreach ($name in @('py', 'python', 'python3')) {
        $candidate = Get-Command $name -ErrorAction SilentlyContinue
        if ($candidate) { return $candidate.Source }
    }
    throw @'
Python was not found on PATH.

Install it from https://www.python.org/downloads/windows/ and tick
"Add python.exe to PATH" in the first installer screen. Then open a NEW
terminal (PATH changes do not reach an already-open one) and re-run:

    .\scripts\windows\setup.ps1
'@
}

Write-Host "kite-alerts setup - $RepoRoot"
Write-Host ''

if (Test-Path -LiteralPath $VenvPython) {
    Write-Host '  venv        : already present, reusing'
} else {
    $base = Get-BasePython -Requested $Python
    $rawVersion = & $base -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    $version = [string]($rawVersion | Select-Object -First 1)
    $version = $version.Trim()
    # Validate before parsing: a wrapper script or a broken interpreter must give
    # this message, not a cast exception from [int]$parts[1].
    if ($version -notmatch '^\d+\.\d+$') {
        throw "Could not read a Python version from '$base' (got '$version')."
    }

    $parts = $version.Split('.')
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    if (($major -lt 3) -or (($major -eq 3) -and ($minor -lt 10))) {
        throw "Python $version was found, but kite-alerts needs 3.10 or newer."
    }

    Write-Host "  python      : $base (Python $version)"
    Write-Host "  venv        : creating $VenvDir"
    & $base -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}

if ($SkipInstall) {
    Write-Host '  install     : skipped (-SkipInstall)'
} else {
    Write-Host '  install     : python -m pip install -e .'
    & $VenvPython -m pip install --disable-pip-version-check -e $RepoRoot
    if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE." }
}

$envPath = [IO.Path]::Combine($RepoRoot, '.env')
$examplePath = [IO.Path]::Combine($RepoRoot, '.env.example')
if (Test-Path -LiteralPath $envPath) {
    Write-Host '  .env        : already present, left untouched'
} else {
    Copy-Item -LiteralPath $examplePath -Destination $envPath
    Write-Host '  .env        : created from .env.example - open it and fill in the values'
}

Push-Location $RepoRoot
try {
    Write-Host '  config-check:'
    & $VenvPython -m kite_alerts.cli.main --env-file $envPath config-check
    if ($LASTEXITCODE -ne 0) { throw "config-check failed with exit code $LASTEXITCODE." }
} finally {
    Pop-Location
}

Write-Host ''
Write-Host 'Next steps'
Write-Host '  1. Edit .env: set KITE_API_KEY, KITE_API_SECRET and SYMBOLS.'
Write-Host '  2. .\scripts\windows\login.ps1     - once per trading day, refreshes the token'
Write-Host '  3. .\scripts\windows\run_bot.ps1   - during market hours'
Write-Host ''
Write-Host 'Full walkthrough, Task Scheduler recipe and troubleshooting: deploy/README.md'
