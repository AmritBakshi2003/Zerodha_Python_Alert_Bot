[![CI](https://github.com/AmritBakshi2003/Zerodha_Python_Alert_Bot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/AmritBakshi2003/Zerodha_Python_Alert_Bot/actions/workflows/ci.yml)

# Kite Connect Trade-Alert Bot (alerts only)

Streams live data from Zerodha Kite Connect, evaluates a blended rule set on every
**closed 1-minute candle**, and pushes a **BUY/SELL alert with a one-line reason**.

**It never places orders.** Only market-data endpoints are used
(`historical_data`, `instruments`, `profile`, and the websocket). This is enforced
mechanically: `tests/test_alerts_only.py` scans the whole package for Kite's
order-side method names, and `.pre-commit-config.yaml` runs it on every commit.

> Not investment advice. The default thresholds are illustrative, not a validated
> strategy. Backtest before relying on them.

## Requirements

- Python 3.10+
- A Kite Connect app (API key + secret) with a paid **historical data** subscription
- One or more alert channels: console, Telegram, Slack or SMTP

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                           # or: pip install -r requirements.txt
cp .env.example .env && chmod 600 .env            # fill in KITE_API_KEY / KITE_API_SECRET / SYMBOLS

kite-alerts config-check                          # validates .env, prints a redacted summary
kite-alerts login                                 # once per trading day (~06:00 IST expiry)
kite-alerts run                                   # during market hours; Ctrl-C to stop
```

Verify the install without any account or network:

```bash
kite-alerts demo        # or: python -m kite_alerts.demo
```

That replays a synthetic day through the real engine and prints one SELL, a second
SELL and one BUY with reasons — the same output as `demo_output.txt`.

## Commands

| Command | Purpose |
|---|---|
| `kite-alerts run` | Live alerting (the production path). `--duration N` stops after N seconds. |
| `kite-alerts login` | Exchange a `request_token` for today's access token and write it to `.env`. |
| `kite-alerts config-check` | Validate configuration, print the redacted snapshot and any warnings. |
| `kite-alerts demo` | Offline synthetic replay; needs no account, no network, no credentials. |
| `kite-alerts ui` | Local dashboard: sidebar pages, practice demo, settings, guided Kite login, history, test alert. Requires the optional `ui` extra. |
| `kite-alerts replay --csv PATH` | Replay a CSV of 1-minute candles through the *same* `Engine`. |
| `kite-alerts replay --from-kite` | Replay recent Kite history for one symbol. |
| `kite-alerts --version` | Print the version. |

`replay` is analysis, not a backtest rig: the report is labelled `ANALYSIS ONLY`,
uses no costs/slippage/sizing, and adds `+5m/+15m/+30m` forward returns per alert.

## Using the dashboard

The dashboard is a local web page for watching the bot, trying the rules and
setting up your own Kite connection. It is optional, reads your data, and never
places orders.

```bash
pip install -e ".[ui]"      # one-time: adds FastAPI + uvicorn
kite-alerts ui --open       # opens http://127.0.0.1:8765 in your browser
```

The left sidebar switches pages (each page has its own `#/` address, so a
bookmark or refresh keeps you there):

- **Dashboard** — whether the bot is running (it asks the bot's existing
  `/health` endpoint), today's BUY and SELL counts, and a live feed of the latest
  alerts with the full reason.
- **Practice** — the demo player, always labelled DEMO. Play, pause and change
  speed to replay a made-up trading day through the real alert engine. A first-run
  welcome panel (dismissed once, remembered in this browser) explains the path:
  try Practice, set up alerts, connect Kite. **Test my settings** replays the same
  day through the engine with *your saved parameters* and shows how many BUY/SELL
  alerts result and which rules agreed. Nothing here is written to your alert
  history.
- **Alerts** — the alerts the bot recorded, filtered by symbol and side, with
  pages. The database is opened read-only, so this page can never change it.
- **Settings** — a form generated from the same pydantic models the bot uses,
  grouped into Instruments, Alert rules, Notifications and Advanced. Labels,
  help, ranges, choices and defaults come from the models. Save writes `.env`
  atomically (temp file then replace), keeps comments and unknown keys, and keeps
  the last five `.env.bak-<timestamp>` copies. Values are validated first; a
  rejected save leaves the file untouched. **Check settings** runs the same
  validation as `kite-alerts config-check`. Changes apply the next time the bot
  starts; the dashboard and Practice use them immediately.
- **Connect** — a four-step wizard for your own Kite Connect app: create it at
  developers.kite.trade, copy the exact loopback Redirect URL it shows, paste the
  API key and secret into write-only fields, then log in on Zerodha's own page.
  There is also a manual `request_token` fallback, a connection status panel with
  Disconnect, and a data-plan check that says whether your plan includes live
  market data. See below.
- **Help** — version, mode, the alerts-only statement, a first-day checklist and
  short troubleshooting.

Notification setup is on Settings: a Telegram guide (@BotFather, press Start),
**Find my chat id** (reads your bot's recent updates and fills the field in),
Slack webhook instructions, channel checkboxes (the terminal is always on), and
one **Send test alert** button that reports each channel separately. The test is
limited to one every 10 seconds and is never recorded as a real alert.

### Bring your own Kite key

Kite Connect is personal-use, so this project never ships a shared API key.
Each person creates **their own** app at [developers.kite.trade](https://developers.kite.trade)
with their own API key and secret, and pays Zerodha directly for whatever plan
they need. Login works on the free Personal tier, but **live market data needs a
paid plan**; check developers.kite.trade for current pricing. The dashboard never
shows a price and never charges you.

Step 2 of the wizard shows the redirect URL for the port you are actually using,
for example `http://127.0.0.1:8765/callback`. Paste that into the Kite app's
Redirect URL field exactly. When you press **Connect to Zerodha**, this app opens
Zerodha's login page in your browser and never sees or stores your password or
TOTP. Zerodha redirects back with a one-time `request_token`; the dashboard
accepts it only while a connect attempt is pending, only with the matching state
value, and only within ten minutes, then exchanges it using the same code as
`kite-alerts login`. The daily access token is saved in `.env` exactly as the CLI
saves it.

If your Kite app refuses a loopback redirect URL, use the **Paste request_token
manually** box: after logging in, copy the `request_token` value from the address
bar and paste it there while the attempt is still pending.

Files the dashboard may write: `.env` (settings and tokens, atomically) and
`.env.bak-<timestamp>` backups (the newest five). Both match `.gitignore`.
Secrets are write-only: the page can show "set" or "not set", never the value,
and no endpoint returns one.

The dashboard only listens on your own computer (`127.0.0.1`), validates the
`Host` header, serves a restrictive CSP with no CDN, requires a same-origin
request for every change, and never logs a token or webhook URL. Its only Kite
calls are read-only: login URL, session exchange, profile, one `ltp` probe and
token invalidation. Starting the bot is still a separate command
(`kite-alerts run`); this version does not start or stop it from the page.

Use `--port` to change the port (then update the redirect URL in your Kite app)
and `--db` to point at a different database. Stop the dashboard with Ctrl-C in
the window where you started it.

### Dashboard troubleshooting

| What you see | What to do |
|---|---|
| Zerodha says **Redirect URL mismatch** | The URL in the Kite app must match the Connect page exactly, including the port. Copy it again from step 2. |
| **Token expired — reconnect** | Kite tokens expire around 06:00 IST. Press Connect again; the bot needs a fresh token each morning too. |
| **Your Kite plan has no market data** | Login worked, but the plan has no live quotes. The delayed chart is used instead; upgrade at developers.kite.trade if you want live data. |
| **Port in use** | Something else is on 8765. Run `kite-alerts ui --port 8766`, then put `http://127.0.0.1:8766/callback` into your Kite app. |
| Settings save says **nothing was saved** | One field is invalid. The message names it; fix that field and save again. The old `.env` is intact. |
| **Telegram has no recent messages** | Open your bot in Telegram, press Start, then press Find my chat id again. |

## Signal logic

Five rules each vote bullish (+1), bearish (−1) or neutral (0). A rule that cannot
be evaluated (e.g. no open interest for an equity) is *unavailable* and drops out of
the vote entirely.

| Rule | Bullish | Bearish |
|---|---|---|
| EMA cross | fast EMA crossed above slow within the last `EMA_CROSS_LOOKBACK` candles | crossed below |
| RSI | RSI within `[RSI_BUY_MIN, RSI_BUY_MAX]` | within `[RSI_SELL_MIN, RSI_SELL_MAX]` |
| MACD | MACD > signal and histogram rising | MACD < signal and histogram falling |
| ORB | close above the opening-range high (first `ORB_MINUTES` from 09:15) | close below the range low |
| Open interest | OI up + price up (long buildup) or OI down + price up (short covering) | OI up + price down (short buildup) or OI down + price down (long unwinding) |

An alert fires when **at least `MIN_AGREE` rules agree and none disagree**.
`MIN_AGREE` is capped to the rules that were actually evaluable, so a missing
indicator can never make alerting impossible. `COOLDOWN_MINUTES` suppresses a
repeat alert on the same side; a side flip is always allowed by default
(`MIN_SIDE_FLIP_MINUTES=0` damps flip-flopping if you raise it).

Optional extras, all no-ops at their defaults: per-rule `RULE_<ID>_WEIGHT` (scales
a vote) and `RULE_<ID>_REQUIRED=true` (that rule **must** vote with the alert side),
plus `MIN_AVAILABLE_RULES` to refuse alerts on a degraded rule set.

Example alert:

```
🟢 BUY DEMO:FUT @ 100.02 [10:07] — EMA9 crossed above EMA21; RSI 72.1 in bullish zone; MACD above signal, histogram rising; OI +1.11% with price up (long buildup)
```

## Configuration

Everything is environment-driven; `.env.example` lists every key with its default.
Secrets are `SecretStr` and never appear in logs, `repr()` or `config-check` output.

### Credentials and instruments
| Key | Default | Meaning |
|---|---|---|
| `KITE_API_KEY` | — | Kite Connect API key (required for live). |
| `KITE_API_SECRET` | — | API secret, used only by `kite-alerts login`. |
| `KITE_ACCESS_TOKEN` | — | Daily token; written by `kite-alerts login`. |
| `SYMBOLS` | — | Comma-separated `EXCHANGE:TRADINGSYMBOL`, e.g. `NSE:INFY,NFO:NIFTY-FUT`. |
| `OVERRIDES_FILE` | — | Optional YAML with per-symbol parameter overrides. |

### Session
| Key | Default | Meaning |
|---|---|---|
| `SESSION_OPEN` / `SESSION_CLOSE` | `09:15` / `15:30` | IST session window; candles outside it are ignored. |
| `HOLIDAY_FILE` | `config/holidays.yaml` | YAML list of NSE holidays. Empty is safe. |
| `WARMUP_DAYS` | `4` | Days of history fetched to seed the indicators. |

### Strategy
Run `kite-alerts config-check` for the redacted, validated set. The main knobs are
`EMA_FAST`/`EMA_SLOW`, `RSI_PERIOD` and the two RSI bands, the `MACD_*` periods,
`ORB_MINUTES`/`ORB_BUFFER_PCT`, `OI_*`, `MIN_AGREE`, `COOLDOWN_MINUTES`,
`WARMUP_BARS`, `MAX_BARS` and `INDICATOR_SEED` (`first` = prototype parity,
`sma` = Wilder/TA-Lib parity).

### Channels
`NOTIFY_CHANNELS` accepts `console`, `telegram`, `slack`, `smtp`.

- **Telegram** (recommended — instant, free, mobile). Create a bot with @BotFather,
  set `TELEGRAM_BOT_TOKEN`, message the bot, read your chat id from
  `https://api.telegram.org/bot<TOKEN>/getUpdates`, set `TELEGRAM_CHAT_ID`, then
  `NOTIFY_CHANNELS=console,telegram`.
- **Slack**: an Incoming Webhook URL in `SLACK_WEBHOOK_URL`.
- **SMTP**: `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_TO`, plus `SMTP_USER` /
  `SMTP_PASSWORD` and `SMTP_USE_TLS` (STARTTLS, on by default).

Delivery tuning: `NOTIFY_RETRIES`, `NOTIFY_TIMEOUT_SECONDS`,
`NOTIFY_MAX_PER_MINUTE`, `NOTIFY_DEDUP_SECONDS`.

### Resilience and observability
| Key | Default | Meaning |
|---|---|---|
| `HISTORY_RATE_LIMIT_PER_SEC` | `3` | Shared token bucket for Kite's historical endpoint. |
| `HISTORY_MAX_RETRIES` | `5` | Bounded, jittered retries; auth/fatal errors are never retried. |
| `RETRY_BASE_SECONDS` / `RETRY_CAP_SECONDS` | `1` / `30` | Backoff base and ceiling. |
| `CIRCUIT_FAILURE_THRESHOLD` / `CIRCUIT_RESET_SECONDS` | `5` / `120` | Breaker trip and half-open trial. |
| `FEED_STALE_SECONDS` | `120` | Feed silence before a notice; reconnect needs two windows. |
| `SYMBOL_STALE_SECONDS` | `600` | Per-instrument silence before that symbol is reported. |
| `RECONNECT_MAX_TRIES` / `RECONNECT_MIN_DELAY` / `RECONNECT_MAX_DELAY` | `25` / `1` / `60` | SDK reconnect policy. |
| `TICK_QUEUE_SIZE` | `20000` | Websocket → worker queue; overflow is dropped **and counted**. |
| `HEARTBEAT_MINUTES` | `60` | "Still alive" message; `0` disables. |
| `HTTP_ENABLED` / `HTTP_HOST` / `HTTP_PORT` | `true` / `127.0.0.1` / `9109` | `/health` and `/metrics`. |
| `HTTP_ALLOW_PUBLIC` | `false` | Must be set explicitly to bind a non-loopback host. |
| `DB_PATH` | `data/kite_alerts.db` | SQLite: alerts, cooldown, dedup ledger. |
| `LOG_DIR` / `LOG_LEVEL` / `LOG_JSON` | `logs` / `INFO` / `false` | Rotating file + stdout, with redaction. |
| `CACHE_DIR` | `cache` | Instrument dumps (`instruments_<EXCHANGE>.csv.gz`). |

## Layout

```
src/kite_alerts/
├── config/         typed, validated settings + per-symbol overrides
├── core/           tz-aware clock, session calendar, models, ports, resilience
├── data/           instruments, history, candle builder, websocket feed, watchdog
├── strategy/       rule registry, the five rules, the voting evaluator
├── engine/         candle history + cooldown + dispatch; the single-owner worker
├── notifiers/      queue/worker hub, retries, dedup, dead-letter log, channels
├── persistence/    SQLite alert store (dedup ledger + engine state)
├── observability/  logging + redaction, Prometheus metrics, /health server
├── backtest/       replay through the same Engine, forward-return summary
├── cli/            `kite-alerts` entry point
└── demo.py         offline synthetic replay
legacy/             the original single-file prototype, reference only
```

## How it behaves

- **Candles close on the clock, not on the next tick.** A candle is emitted when the
  next minute's first tick arrives *or* when the worker's minute timer fires, so an
  illiquid symbol's last candle of the day still closes.
- **Volume is derived from cumulative deltas.** Kite's `volume_traded` is cumulative
  for the day; the per-candle volume is the delta between the first and last tick of
  that minute, clamped at zero.
- **Timestamps are IST, always.** Naive Kite timestamps are *interpreted* as IST;
  nothing consults the host timezone, so a UTC container behaves identically.
- **State survives restarts.** The SQLite `alerts` table is simultaneously the dedup
  ledger, the cooldown source and the audit trail, so a restart does not re-send.
- **A failing channel cannot block the data path.** Notifications are enqueued; one
  worker thread delivers with per-channel rate limits and bounded retries. Final
  failures go to `logs/dead-letter.jsonl`.
- **Auth failures stop the run once.** An expired token produces one `🔐` alert and a
  clean `EXIT_AUTH` (3), never a retry loop.
- **A silent feed is detected.** The watchdog reports feed-level and per-symbol
  silence, then performs at most one controlled reconnect per few windows.

## Runbook

**Start of day**

```bash
kite-alerts login                 # refresh the token (expires ~06:00 IST)
kite-alerts config-check          # confirm the config still parses
kite-alerts run                   # leave it in tmux/systemd
```

**Is it healthy?**

```bash
curl -s localhost:9109/health | python -m json.tool   # 200 = ok, 503 = unhealthy
curl -s localhost:9109/metrics | grep kite_alerts_
```

`/health` reports `status` (`ok` / `stale_feed` / `auth_failed`), the session state,
the age of the last tick, queue depth, reconnect count and notifier counters.

**An alert never arrived**

1. `logs/alerts-audit.jsonl` — one JSON object per fired alert, with the parameter
   snapshot and the rules that voted.
2. `logs/dead-letter.jsonl` — undeliverable messages with channel, attempts and error.
   Re-send by hand from the payload.
3. `sqlite3 data/kite_alerts.db "select ts, symbol, side, price from alerts order by id desc limit 10"`.
4. Cooldown: a same-side repeat inside `COOLDOWN_MINUTES` is suppressed on purpose
   (`alerts_suppressed_cooldown` in `/metrics`).

**`EXIT_AUTH` (3)** — the token expired or was revoked. Run `kite-alerts login` and
restart. **`EXIT_CONFIG` (2)** — the message names the offending key.

**Expired or rolling contract** — use the alias `NFO:NIFTY-FUT` instead of a hard-coded
tradingsymbol; it resolves to the nearest **unexpired** expiry and warns at startup
when a contract has ≤2 days left.

## Robustness

- Bounded exponential backoff **with jitter**, capped; auth and fatal errors are never
  retried.
- One shared token bucket keeps historical calls inside Kite's ~3 req/s limit.
- A circuit breaker opens on repeated dependency failures and half-opens one trial.
- Websocket ticks only enqueue; a full queue drops and counts, never blocks the socket.
- After a reconnect, the gap is back-filled without re-alerting already-seen minutes.
- Rule exceptions are logged and treated as "unavailable", never as a crash.

## Development

```bash
pip install -e ".[dev]"
pytest --cov=kite_alerts          # coverage threshold is enforced (85%)
ruff check && black --check src tests && mypy
pre-commit install                # ruff, black, hygiene hooks + alerts-only guard
```

`tests/fakes/fake_kite.py` provides `FakeKite` / `FakeTicker`, so the entire live
pipeline (feed → queue → candle builder → engine → notifier) is exercised offline.
