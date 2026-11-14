<#
.SYNOPSIS
    Supervised live run of kite-alerts on Windows. No Docker, no extra tools.

.DESCRIPTION
    Runs `kite-alerts run` under a supervising loop:

      * restarts after a crash with exponential backoff, up to -MaxRestarts;
      * never restarts on a configuration (exit 2) or authentication (exit 3)
        failure - those need a human: fix .env, or refresh the daily token;
      * passes --duration so the bot shuts itself down cleanly shortly after
        SESSION_CLOSE instead of being killed mid-write;
      * stays out of the way on a weekend or after the close, and waits if it
        is started shortly before 09:00 IST.

    Supervisor activity is appended to <LOG_DIR>\supervisor-YYYYMMDD.log, next
    to the bot's own logs. Exit codes: 0 clean stop, 1 gave up after the restart
    limit, 2 configuration error, 3 authentication error.

.PARAMETER EnvFile
    Environment file handed to the bot. Default: .env in the repository root.

.PARAMETER StopAt
    IST time (HH:MM) at which the bot stops. Default: SESSION_CLOSE from .env
    plus five minutes.

.PARAMETER MaxRestarts
    Consecutive crash restarts before giving up. Default: 5.

.PARAMETER BackoffBaseSeconds
    First restart delay; doubles per attempt. Default: 5.

.PARAMETER BackoffMaxSeconds
    Cap for the restart delay. Default: 300.

.PARAMETER MaxWaitForOpenMinutes
    How long this script will sit waiting for 09:00 IST before telling you to
    start it later. Default: 120.

.PARAMETER Once
    Single attempt, no restarts.

.PARAMETER Force
    Skip the trading-day and market-hours gate. Use it for a supervised trial
    (e.g. -Force -Once), never for the unattended daily run.

.EXAMPLE
    .\scripts\windows\run_bot.ps1

.EXAMPLE
    .\scripts\windows\run_bot.ps1 -Force -Once
#>
[CmdletBinding()]
param(
    [string]$EnvFile = '.env',
    [string]$StopAt = '',
    [int]$MaxRestarts = 5,
    [int]$BackoffBaseSeconds = 5,
    [int]$BackoffMaxSeconds = 300,
    [int]$MaxWaitForOpenMinutes = 120,
    [switch]$Once,
    [switch]$Force
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
$EnvPath = if ([IO.Path]::IsPathRooted($EnvFile)) {
    $EnvFile
} else {
    [IO.Path]::Combine($RepoRoot, $EnvFile)
}

function Get-IstNow {
    # India has never observed DST, so a fixed +05:30 offset is exact and needs
    # no timezone database lookup. It also keeps the script correct on a PC that
    # is set to a different local timezone than IST.
    return [DateTime]::UtcNow.AddHours(5.5)
}

function Read-EnvValue {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Key,
        [string]$Default = ''
    )

    if (-not (Test-Path -LiteralPath $Path)) { return $Default }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $text = $line.Trim()
        if ($text.Length -eq 0 -or $text.StartsWith('#')) { continue }
        $split = $text.IndexOf('=')
        if ($split -lt 1) { continue }
        if ($text.Substring(0, $split).Trim() -ne $Key) { continue }

        $value = $text.Substring($split + 1).Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        return $value
    }
    return $Default
}

$LogDirSetting = Read-EnvValue -Path $EnvPath -Key 'LOG_DIR' -Default 'logs'
$LogDir = if ([IO.Path]::IsPathRooted($LogDirSetting)) {
    $LogDirSetting
} else {
    [IO.Path]::Combine($RepoRoot, $LogDirSetting)
}
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$SupervisorLog = [IO.Path]::Combine($LogDir, ('supervisor-{0}.log' -f (Get-IstNow).ToString('yyyyMMdd')))

function Write-BotLog {
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )

    $line = '{0} [{1}] {2}' -f (Get-IstNow).ToString('yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Host $line
    Add-Content -LiteralPath $SupervisorLog -Value $line -Encoding UTF8
}

$CloseText = Read-EnvValue -Path $EnvPath -Key 'SESSION_CLOSE' -Default '15:30'
try {
    if ($StopAt) {
        $StopSpan = [TimeSpan]::Parse($StopAt)
    } else {
        $StopSpan = [TimeSpan]::Parse($CloseText).Add([TimeSpan]::FromMinutes(5))
    }
} catch {
    Write-BotLog "cannot read the stop time (StopAt='$StopAt', SESSION_CLOSE='$CloseText'); use HH:MM." 'ERROR'
    exit 2
}
if ($StopSpan.TotalDays -ge 1) {
    Write-BotLog "stop time $StopSpan crosses midnight; clamping to 23:59." 'WARN'
    $StopSpan = [TimeSpan]::FromHours(23).Add([TimeSpan]::FromMinutes(59))
}

$stopLabel = $StopSpan.ToString('hh\:mm')
Write-BotLog "supervisor starting (stop at $stopLabel IST, repo $RepoRoot)"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-BotLog 'no virtual environment found; run .\scripts\windows\setup.ps1 first.' 'ERROR'
    exit 1
}

if (-not $Force) {
    $now = Get-IstNow
    if ($now.DayOfWeek -eq [DayOfWeek]::Saturday -or $now.DayOfWeek -eq [DayOfWeek]::Sunday) {
        Write-BotLog ("{0} is a {1}; NSE is closed, nothing to do." -f $now.ToString('yyyy-MM-dd'), $now.DayOfWeek)
        exit 0
    }
    if ($now.TimeOfDay -ge $StopSpan) {
        Write-BotLog "it is already $($now.ToString('HH:mm')) IST, past $stopLabel; the market is closed."
        exit 0
    }

    $openWait = [TimeSpan]::FromHours(9) - $now.TimeOfDay
    if ($openWait.TotalSeconds -gt 0) {
        if ($openWait.TotalMinutes -gt $MaxWaitForOpenMinutes) {
            Write-BotLog ("it is {0} IST and the session opens at 09:15: waiting would take {1:N0} min (limit {2:N0}). Start the bot closer to the open, or use the Task Scheduler recipe in deploy/README.md." -f $now.ToString('HH:mm'), $openWait.TotalMinutes, $MaxWaitForOpenMinutes) 'WARN'
            exit 0
        }
        Write-BotLog ("before 09:00 IST; waiting {0:N0} min for the session to open." -f $openWait.TotalMinutes)
        while ((Get-IstNow).TimeOfDay -lt [TimeSpan]::FromHours(9)) {
            Start-Sleep -Seconds 30
        }
    }
    Write-BotLog 'session window reached; starting the bot.'
}

if ($Once) {
    $maxAttempts = 1
} else {
    $maxAttempts = $MaxRestarts + 1
}

$attempt = 0
while ($true) {
    $remaining = ($StopSpan - (Get-IstNow).TimeOfDay).TotalSeconds
    if (-not $Force -and $remaining -le 0) {
        Write-BotLog "past $stopLabel IST; stopping."
        exit 0
    }

    $runArgs = @('-m', 'kite_alerts.cli.main', '--env-file', $EnvPath, 'run')
    if ($remaining -gt 0) {
        # Letting the bot enforce the deadline itself means it exits through its
        # own shutdown path (SQLite closed, "stopped" written to the audit log)
        # instead of being terminated at market close.
        $runArgs += @('--duration', [string][int][Math]::Floor($remaining))
    }

    $attempt++
    Write-BotLog ("starting the bot (attempt {0} of {1})" -f $attempt, $maxAttempts)

    $code = 1
    Push-Location $RepoRoot
    try {
        & $VenvPython @runArgs
        $code = $LASTEXITCODE
    } catch {
        Write-BotLog ("could not start the bot: {0}" -f $_.Exception.Message) 'ERROR'
        $code = 1
    } finally {
        Pop-Location
    }
    Write-BotLog "the bot exited with code $code"

    if ($code -eq 0) {
        Write-BotLog 'clean exit (market close, duration reached or Ctrl-C); supervisor done.'
        exit 0
    }
    if ($code -eq 2) {
        Write-BotLog 'configuration error (exit 2); not restarting. Fix .env (KITE_API_KEY, KITE_API_SECRET, SYMBOLS) and start again.' 'ERROR'
        exit 2
    }
    if ($code -eq 3) {
        Write-BotLog 'authentication error (exit 3); not restarting. Run .\scripts\windows\login.ps1, then start the bot again.' 'ERROR'
        exit 3
    }

    if ($attempt -ge $maxAttempts) {
        Write-BotLog ("giving up after {0} attempt(s); the last exit code was {1}." -f $attempt, $code) 'ERROR'
        exit 1
    }

    $delay = [int][Math]::Min($BackoffBaseSeconds * [Math]::Pow(2, $attempt - 1), $BackoffMaxSeconds)
    if ($delay -lt 0) { $delay = 0 }
    Write-BotLog ("crash (exit {0}); restarting in {1}s." -f $code, $delay) 'WARN'
    if ($delay -gt 0) { Start-Sleep -Seconds $delay }
}
