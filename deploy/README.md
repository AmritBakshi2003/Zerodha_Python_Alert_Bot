# Running kite-alerts on your own machine

This bot runs directly on a normal PC with a Python virtual environment. There is
**no Docker, no container, no server and no cloud account** involved, and the
scripts need nothing beyond PowerShell that ships with Windows.

It is an **alerts-only** bot: it never places, modifies or cancels an order, and
it never touches your holdings. `tests/test_alerts_only.py` enforces that in CI.

- **Windows:** [the ten-minute setup](#windows-setup)
- **Linux / macOS:** [quickstart](#linux--macos)
- **Unattended on a VPS:** [systemd](#systemd-on-a-vps-instead-of-a-windows-pc)
- **Why not GitHub Actions?** [read this before trying it](#why-not-run-the-live-bot-on-github-actions)

---

## Windows setup

### 1. Install Python

1. Go to <https://www.python.org/downloads/windows/> and download the latest
   **Windows installer (64-bit)** for Python 3.12 or newer.
2. On the **first** installer screen, **tick "Add python.exe to PATH"** before
   clicking *Install Now*. This is the step people skip, and then nothing works.
3. Open a **new** PowerShell window (PATH changes never reach an already-open
   one) and check:

   ```powershell
   python --version
   ```

   You should see `Python 3.12.x` or newer. `py --version` also works.

### 2. Get the code

```powershell
cd $HOME
git clone https://github.com/AmritBakshi2003/Zerodha_Python_Alert_Bot.git
cd Zerodha_Python_Alert_Bot
```

No git? Install [Git for Windows](https://git-scm.com/download/win), or download
the ZIP from the repository page and extract it — everything else works the same.

If PowerShell refuses to run the scripts ("running scripts is disabled on this
system"), allow locally-created scripts once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 3. Run setup

```powershell
.\scripts\windows\setup.ps1
```

It is **idempotent**: it creates `.venv`, installs the package in editable mode,
copies `.env.example` to `.env` only if `.env` does not exist yet, and finishes
by running `config-check`. Re-run it after every update; it will not overwrite
your settings.

### 4. Fill in `.env`

Open `.env` in Notepad and set at least:

| Key | What it is |
| --- | --- |
| `KITE_API_KEY` | From your Kite Connect app at <https://developers.kite.trade/> |
| `KITE_API_SECRET` | Same app; **keep private** |
| `SYMBOLS` | Comma-separated, e.g. `NSE:RELIANCE,NFO:NIFTY-FUT` |
| `NOTIFY_CHANNELS` | `console`, `telegram`, `slack` and/or `smtp` |

`NFO:NIFTY-FUT` is a convenience alias that resolves to the nearest expiry from
the instrument dump; you can also name a contract exactly
(`NFO:NIFTY26NOVFUT`). `.env` is git-ignored — never commit it, and never paste
the secret into an issue.

Re-check any time with:

```powershell
.\.venv\Scripts\python.exe -m kite_alerts.cli.main config-check
```

### 5. Log in once per trading day

Kite access tokens expire daily at about **06:00 IST**. Every trading morning:

```powershell
.\scripts\windows\login.ps1
```

It prints a Kite URL, you approve it, paste back the `request_token` (or the
whole redirected URL), and the token is written into `.env` safely, leaving the
rest of the file untouched.

### 6. Run during market hours

```powershell
.\scripts\windows\run_bot.ps1
```

The supervisor:

- refuses to start on a weekend or after the close, and tells you why;
- waits up to two hours if you start it before 09:00 IST;
- lets the bot stop **itself** cleanly a few minutes after `SESSION_CLOSE`
  (default 15:35 IST) — it is not killed mid-write;
- restarts it after a crash with exponential backoff (5s, 10s, 20s … capped at
  300s, 5 restarts by default);
- **does not restart** on a configuration or token problem, because retrying
  those all morning changes nothing.

`Ctrl-C` stops it cleanly. For a short supervised trial outside market hours:

```powershell
.\scripts\windows\run_bot.ps1 -Force -Once
```

### 7. Keep the PC awake

A sleeping PC silently stops the feed. As an administrator:

```powershell
powercfg /change standby-timeout-ac 0      # never sleep while plugged in
powercfg /change hibernate-timeout-ac 0    # never hibernate while plugged in
powercfg /change monitor-timeout-ac 10     # screen may still sleep
```

Laptop lid: *Control Panel → Power Options → Choose what closing the lid does →
Do nothing* (plugged in only). Then confirm nothing else is asking the machine to
sleep:

```powershell
powercfg /requests
```

Also turn off *Settings → Windows Update → Advanced options → Restart this device
as soon as possible…* during market hours, and avoid running the bot on battery.

### 8. Start it automatically on weekdays

Create a scheduled task that starts the supervisor at **08:55 IST** on weekdays:

```powershell
schtasks /Create /TN "kite-alerts" `
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\<you>\Zerodha_Python_Alert_Bot\scripts\windows\run_bot.ps1" `
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:55 /RL HIGHEST /F
```

Replace the path with your real one (`(Get-Location).Path` in the repo prints it).
Notes that matter:

- `/ST 08:55` is **system local time**. The task fires at 08:55 on the PC's clock,
  so the PC must be set to IST — otherwise adjust the time.
- The task runs as you, when you are logged on. Tick *Run whether user is logged
  on or not* in Task Scheduler's GUI if you want it to survive a sign-out.
- The scripts resolve every path from their own location, so the task's *Start in*
  directory does not matter.
- Login is still manual (see step 5). The scheduled task will stop immediately
  with a clear message in `logs\` if the token was never refreshed that day.

Remove it with `schtasks /Delete /TN "kite-alerts" /F`.

### 9. Where your data lives

| Path | Contents |
| --- | --- |
| `data/kite_alerts.db` | SQLite: fired alerts, dedup/cooldown state. **The file to back up.** |
| `data/kite_alerts.db-wal`, `-shm` | SQLite write-ahead log; part of the database |
| `logs/kite-alerts.log` | Rotating application log (10 MB × 5) |
| `logs/supervisor-YYYYMMDD.log` | What the supervisor did: starts, crashes, exit codes |
| `logs/alerts-audit.jsonl` | One JSON line per fired alert, with the parameter snapshot |
| `logs/dead-letter.jsonl` | Alerts that could not be delivered to a channel |
| `cache/` | Kite instrument dump cache; safe to delete, it is re-downloaded |

Credentials are **not** in any of these: log output is passed through a redaction
filter, and `.env` stays outside the data directory.

**Backing up:** stop the bot first (the `-wal` file must be quiesced), then copy
`data\` somewhere else:

```powershell
Copy-Item -Recurse -Force .\data "$env:USERPROFILE\Backups\kite-alerts-data-$(Get-Date -Format yyyyMMdd)"
```

Restoring is the reverse: put the folder back with the bot stopped.

### 10. Updating

```powershell
cd $HOME\Zerodha_Python_Alert_Bot
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -e .
```

Then re-run `.\scripts\windows\login.ps1` and start the bot. `setup.ps1` also
works and additionally re-validates the configuration. Update outside market
hours; `git pull` while the bot runs will not reload the code anyway.

### 11. When something looks wrong

**Is it alive?** With `HTTP_ENABLED=true` (the default) the bot serves health on
loopback only:

```powershell
Invoke-RestMethod http://127.0.0.1:9109/health | ConvertTo-Json -Depth 4
```

Read these fields: `status` (`ok` / `stale_feed` / `auth_failed`),
`in_session`, `last_tick_age_seconds`, `reconnects`.

**After a websocket reconnect** — the feed reconnects on its own, so the useful
question is whether data resumed:

1. `reconnects` went up, and `last_tick_age_seconds` came back down to single
   digits → it recovered; nothing to do.
2. `status: stale_feed` while `in_session: true` → no ticks for longer than
   `FEED_STALE_SECONDS` (120s). Check the internet connection, then the log for
   `reconnect` lines.
3. `status: auth_failed` → the token expired or was revoked. Run `login.ps1` and
   restart. `run_bot.ps1` stops rather than retrying this case.
4. `symbols` shorter than `SYMBOLS` in `.env` → an instrument did not resolve
   (renamed, expired contract, or a symbol typo). The log names the reason.
5. No ticks at all and `in_session: false` → the market is closed, or the PC's
   clock/timezone is wrong. Every session decision is made on IST event time.

The bot explains itself in `logs\kite-alerts.log`; a missing historical-data
entitlement, an expired token and a resolved-nothing symbol list each produce a
distinct message.

---

## Linux / macOS

Same three steps, same behaviour, bash instead of PowerShell:

```bash
git clone https://github.com/AmritBakshi2003/Zerodha_Python_Alert_Bot.git
cd Zerodha_Python_Alert_Bot

./scripts/unix/setup.sh        # venv + install + .env + config-check
$EDITOR .env                   # API key, secret, symbols, channels
./scripts/unix/login.sh        # once per trading day
./scripts/unix/run_bot.sh      # during market hours
```

They accept the same options: `--stop-at 15:35`, `--max-restarts 5`,
`--once`, `--force`. On Debian/Ubuntu install `python3` and `python3-venv`
first.

### systemd on a VPS instead of a Windows PC

A plain venv install, no container. Copy the unit and adapt the paths:

```bash
sudo useradd --system --home /opt/kite-alerts --shell /usr/sbin/nologin kite-alerts
sudo git clone https://github.com/AmritBakshi2003/Zerodha_Python_Alert_Bot.git /opt/kite-alerts
sudo -u kite-alerts python3 -m venv /opt/kite-alerts/.venv
sudo -u kite-alerts /opt/kite-alerts/.venv/bin/pip install -e /opt/kite-alerts

sudo install -d -m 0750 -o root -g kite-alerts /etc/kite-alerts
sudo install -m 0640 -o root -g kite-alerts .env /etc/kite-alerts/kite-alerts.env
sudo install -m 0644 deploy/systemd/kite-alerts.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kite-alerts
```

`EnvironmentFile` is the systemd way to pass `.env`; `data/`, `logs/` and
`cache/` are the only writable paths (`ProtectSystem=strict` makes the rest
read-only). The unit uses `RestartPreventExitStatus=2 3`, so a broken config or a
dead token stops the unit instead of looping, and `KillSignal=SIGINT` so the
shutdown path runs.

On a VPS you still have to refresh the token daily — `kite-alerts login` is
interactive by design, so run it over SSH with `systemctl restart kite-alerts`
afterwards.

---

## Exit codes

`kite-alerts run` and the supervisors agree on these:

| Code | Meaning | What happens |
| --- | --- | --- |
| `0` | Clean stop (market close, duration reached, Ctrl-C) | Supervisor exits 0 |
| `1` | Unexpected error | Supervisor restarts with backoff |
| `2` | Configuration problem (bad `.env`, missing credentials) | **No restart**; supervisor exits 2 |
| `3` | Authentication (missing or expired token, revoked access) | **No restart**; run `login`, supervisor exits 3 |
| `4` | Data problem (nothing resolvable / no historical data) | Supervisor restarts with backoff |

Codes 2 and 3 are treated as terminal on purpose: both need a human, and a
restart loop would only delay the message.

---

## Why not run the live bot on GitHub Actions?

It is tempting — free minutes, nothing to keep awake. It does not work for a live
trading feed:

- **Logs on a public repo are public.** The owner's repository is public, and
  workflow logs are visible to anyone. Alert messages, symbols and timings would
  leak.
- **Six-hour job limit.** GitHub kills a job at 6 hours; an NSE session plus
  warm-up, and certainly an unattended daily schedule, does not fit around that.
- **No persistent state.** Each run gets a fresh machine: the SQLite alert store,
  dedup/cooldown history, the audit log and the instrument cache are all gone at
  the end, so deduplication silently resets every morning and the audit trail
  cannot be kept.
- **The daily login is manual.** `kite-alerts login` needs a browser and a
  request_token typed back in. There is no unattended Kite flow, and automating
  it with the API secret in a scheduled public job is exactly the wrong trade.
- **No long-lived websocket.** Even inside 6 hours, runners are not guaranteed to
  stay up for a full session, and a dropped runner means a silently missed
  morning with no one watching.

CI here does the opposite job: it runs the **offline** demo, the test suite and
the golden-output diff so a change cannot break the code. It never starts the
live bot, never prints an alert from a real account, and references no Kite,
Telegram or Slack secrets.
