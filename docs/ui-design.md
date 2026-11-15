# Dashboard design (C1 + C2 + C3 shipped)

C1 shipped a local, read-only dashboard with an offline demo player. C2 adds the
sidebar layout, the settings form, guided notifications, and the
"bring your own Kite key" connection flow. C3 adds the Chart landing page:
seven chart types, 1/3/5/15-minute timeframes, server-computed indicators, two
free honest data sources (delayed NSE and a crypto showcase), the Demo replay,
a watchlist, a five-rule signal meter, an IST market clock and PNG export.
Kite live streaming remains C4.

Nothing here changes the signal rules, the engine, or the trading path: the
project remains alerts-only and has no order API.

## Layering

```
kite_alerts/ui/
├── app.py              FastAPI factory, UiContext, JSON routes
├── security.py         loopback bind check, Host validation, CSP, origin check
├── history.py          read-only SQLite reader (mode=ro + PRAGMA query_only)
├── status.py           probes the bot's existing /health endpoint
├── demo_player.py      synthetic day through the real Engine (+ saved params)
├── series.py           EMA/RSI/MACD/ORB series from the bot's own indicators
├── test_alert.py       rate-limited TEST delivery with per-channel results
├── settings_schema.py  form metadata derived from the pydantic models
├── settings_store.py   atomic .env writes, validation, backups, rate limit
├── kite_connect.py     pending login, token exchange, session, data probe
├── notifications.py    Telegram getUpdates -> chat id
├── server.py           validate host/port, build the app, run uvicorn
├── chart/
│   ├── types.py        Candle/SourceSeries/SymbolInfo + plain errors
│   ├── transforms.py   true epochs, 3m resampling, Heikin-Ashi
│   ├── resilience.py   cache, coalescing, jittered backoff, breaker
│   ├── symbols.py      NSE/NIFTY/BANK NIFTY -> Yahoo tickers; NFO refused
│   ├── sources.py      DelayedNSE, CryptoShowcase, Demo, Kite placeholder
│   └── service.py      indicators, freshness, signal meter, clock, watchlist
└── static/
    ├── index.html      sidebar + seven page sections
    ├── styles.css      light/dark, keyboard focus, responsive, badges
    ├── app.js          hash router, dashboard, history, help
    ├── dom.js          textContent-only DOM helpers and the fetch wrapper
    ├── chart.js        Chart page: picker, series, polling, export, meter
    ├── practice.js     demo player, first-run welcome, "Test my settings"
    ├── settings.js     generated form, save/check, notification guide
    ├── connect.js      wizard, status, manual fallback, data sources
    └── vendor/         vendored chart library + licence
```

`kite_alerts.ui.__init__` imports nothing heavy, and `cli/main.py` imports
`ui.server` lazily. Installing the bot without the `ui` extra therefore never
touches FastAPI or uvicorn.

Every route gets its dependencies from one explicitly constructed `UiContext`,
which is how the tests inject a fake Kite client, a fake Telegram endpoint, a
fake probe, a fake notifier factory and a temporary database. `UiContext.reload()`
re-reads `.env` after a save or a connect/disconnect so every page sees the new
state without a restart.

## Layout and routing

`index.html` is one document with a `<nav aria-label="Pages">` sidebar and seven
`<section data-page="…">` panels. The hash is the only router state:

`#/chart`, `#/dashboard`, `#/practice`, `#/alerts`, `#/settings`, `#/connect`,
`#/help`.

`app.js` maps an unknown or empty hash to Chart, toggles `hidden`, sets
`aria-current="page"` on the active link, updates the title, and lazily
initialises each page module on first visit. A skip link targets `<main
tabindex="-1">`; every control is a real button/input/select with a label, and
focus is visible. The pages are plain enough to work without a build step and
are served by CSP `script-src 'self'`.

The Dashboard refreshes its status and feed every five seconds only while it is
the current page. Practice, Settings and Connect initialise once and then keep
their state; returning to Settings while the form is dirty does not discard
edits.

## API surface

| Route | Method | Purpose |
|---|---|---|
| `/api/status` | GET | bot health + today's counts |
| `/api/alerts/recent` | GET | latest alerts |
| `/api/alerts/history` | GET | filters + pagination (read-only DB) |
| `/api/demo/timeline` | GET | full demo timeline |
| `/api/demo/stream` | GET | NDJSON replay |
| `/api/test-alert` | POST | rate-limited TEST delivery |
| `/api/about` | GET | version, mode, checklist, connection chip |
| `/api/settings` | GET | generated form metadata, **no secret values** |
| `/api/settings` | PUT | validate + atomic `.env` write |
| `/api/settings/check` | POST | same validation as `config-check`, redacted |
| `/api/kite/login` | POST | begin a login attempt; returns `/kite/start` |
| `/kite/start` | GET | 302 to Zerodha's login URL (not in OpenAPI) |
| `/callback` | GET | loopback redirect; validates state, exchanges token |
| `/api/kite/manual-token` | POST | same exchange for a pasted `request_token` |
| `/api/kite/session` | GET | `Connected as <name>` / not connected / expired |
| `/api/kite/disconnect` | POST | invalidate + clear the saved token |
| `/api/data-sources` | GET | availability, badge, notice and reason per source |
| `/api/chart/symbols` | GET | allowlisted symbols for a source |
| `/api/chart/candles` | GET | OHLCV + indicators, freshness, signal |
| `/api/chart/live` | GET | the last two candles for the forming-candle poll |
| `/api/chart/clock` | GET | IST open/pre-open/closed + countdown |
| `/api/chart/watchlist` | GET/POST/DELETE | read/add/remove symbols |
| `/api/notifications/telegram/chat-id` | POST | read the bot's recent update |
| `/api/practice/check` | POST | replay the demo with saved parameters |

All state-changing routes call `require_same_origin`. `/kite/start` is a GET
redirect but only ever forwards to the URL created by the immediately preceding
same-origin `POST /api/kite/login`.

`GET /api/kite/login` deliberately does **not** exist: the login URL contains the
API key, so it is never serialised. `POST /api/kite/login` returns
`start_url: "/kite/start"`, and the browser navigates there; the 302's `Location`
is the only place the key appears, and JavaScript on this origin cannot read it.

## Settings

**Metadata from the pydantic models.** `settings_schema.py` defines an explicit
env-key → model-path table (`KEY_SPECS`) with plain-language labels, help text,
units and group assignment. Bounds (`ge`/`le`), default values and enum choices
are read from the live `FieldInfo`, so a new pydantic field or a changed bound
shows up in the form. The table is tested to cover every key the loader accepts
(`env_warnings == ()` for a file built from all defaults).

Groups are Instruments, Alert rules, Notifications and Advanced. Per-rule
`ENABLED`/`WEIGHT`/`REQUIRED` controls come from `RULE_ENV_PREFIX` entries; the
existing models do not define per-symbol rule overrides, so the page links to
`OVERRIDES_FILE` rather than inventing one. Advanced fields render inside a
`<details>` block to keep the first screen short.

**Validation.** `PUT /api/settings` accepts `{"values": {ENV_KEY: value}}`. The
store reads the current file, replaces or appends only those keys, writes
`.env.tmp` in the same directory, and calls `load_settings(temp, environ={})`.
Pydantic's `ValidationError` is formatted and its location (`params.ema_fast`,
`ema_fast`, `rules.rsi_zone.weight`, `notify.channels`) is mapped back to the
form's env key by `key_for_path`; a `ConfigError` from a typed reader is attached
to the key named in its message. Nothing is replaced unless the whole model
validates. Unknown keys and the write-only access token are refused by the
allowlist; corrupted input returns 422 with the original file untouched.

**Atomic write with backups.** On success:
1. read the existing `.env` as text (comments and unknown keys preserved);
2. copy it to `.env.bak-<UTC timestamp>` (pruned to the newest five);
3. write `.env.tmp`, `flush`, `fsync`, then `os.replace()` it over `.env`;
4. remove the temp file on any failure and leave the original intact;
5. reload the context and return `saved`, `backup` and the applies notice.

Saves are limited to one per second per process (`Retry-After` on 429). `.env`,
`.env.*` and the backups are git-ignored, with a test that fails if that changes.

**Secrets are write-only.** `field_payload(..., for_settings_page=True)` emits
`{"secret": true, "is_set": true}` and no `value`. An empty string clears a
secret; omitting the key leaves it unchanged. An empty submitted secret field is
omitted by the browser. `KITE_ACCESS_TOKEN` is marked `connect_only` and can only
be written by the connect flow, never by the settings form.

**Check settings.** `POST /api/settings/check` runs `load_settings` and returns
`ok`, a plain message, warnings and the same redacted summary as
`kite-alerts config-check` (no token, secret or full webhook URL).

## Connect: bring your own key

Each person creates their own app at developers.kite.trade. The wizard shows
that link, says live data needs a paid plan and to check the site for current
pricing (no price is hardcoded), and shows the exact redirect URL derived from
the running port: `http://127.0.0.1:<port>/callback`.

**State machine.** `POST /api/kite/login`:
1. refuses with `missing_key` / `missing_secret` (with guidance) or `settings`;
2. builds the URL with `KiteConnect.login_url()` (the only way the SDK is used
   here) and stores `{state, created, url}` in memory;
3. returns `start_url="/kite/start"`, `redirect_url`, `expires_in=600`;
4. sets an HttpOnly, SameSite=Lax `ka_connect_state` cookie.

`GET /callback?request_token=…[&state=…|&status=…]`:
- `status` other than `success` cancels the pending attempt and redirects with
  `?connect=cancelled`;
- effective state is the query `state` if present, otherwise the cookie;
- `finish()` rejects `no_attempt` (nothing pending), `state_expired` (older than
  ten minutes; the attempt is dropped), and `bad_state` (missing/wrong, compared
  with `compare_digest`);
- then it extracts the token with `cli/login_cmd.extract_request_token`, calls
  `generate_session`, and writes the token with
  `cli/login_cmd.write_access_token` — the same functions the CLI uses. No
  login logic is duplicated;
- success redirects to `/?connect=connected#/connect`, deletes the cookie and
  clears the cached profile/data; failures redirect with a stable code that
  `connect.js` turns into a plain sentence. The token is never in the URL.

The pending attempt is single-use: it is cleared on success, on exchange failure
and on cancellation. A missing/short `request_token` is `missing_token`; a Kite
token rejection is `exchange_failed` (the exception text is not echoed); a
network failure is `unreachable`.

**Manual fallback.** The Connect page has a `<details>` box, "Zerodha didn't come
back? Paste the request_token instead". It posts to `/api/kite/manual-token` with
the same pending-state validation and the same exchange path, so the fallback
cannot bypass the state check. Whether Kite's app console accepts an
`http://127.0.0.1` redirect URL is **UNVERIFIED**; the manual box exists for that
case.

**Session panel.** `GET /api/kite/session` returns one of:
- `connected` with the `user_name` from a read-only `profile()` call;
- `not_connected` (no token, or settings unreadable);
- `expired` when Kite rejects the token;
- `unreachable` when the network fails.
The profile is cached for 60 seconds per token. The payload includes
`expires_note = "expires about 06:00 IST"`, `redirect_url`, `token_present` and
the write-only credential pills. Disconnect calls `invalidate_access_token`
(best effort) and always clears `KITE_ACCESS_TOKEN` locally.

**Data-plan probe.** `GET /api/data-sources` makes at most one `ltp(["NSE:NIFTY
50"])` call per 60 seconds (or on `?refresh=true`) and maps the result:

| Result | `kite_live.available` | Message |
|---|---|---|
| data returned | true | "Live data available from your Kite account." |
| permission error | false | "Your Kite plan has no market data … the delayed chart will be used." |
| token error | false | "Token expired. Reconnect …" |
| rate limit | false | "Kite is rate limiting …" |
| network | false | "Kite is unreachable …" |
| empty/other | false | plain "could not confirm" message |

Delayed NSE, the crypto showcase and the practice demo are always reported
available with a reason. Each source now also carries `badge`, `enabled`,
`notice` and `supports_oi`; `kite_live.enabled` is always **false** in C3, even
when the account has a market-data plan.

## Chart workstation

**One source interface, three real adapters.** `chart/sources.py` defines
`ChartSource`: `id`, `label`, `badge`, `supports_oi`, `enabled`, `session_based`,
`symbols()` and `fetch()`. `DelayedNSESource` maps NSE equities to `<TICKER>.NS`
and the two indices to `^NSEI`/`^NSEBANK` through `yfinance` (Apache-2.0, lazily
imported); NFO is refused with a reason. `CryptoShowcaseSource` calls Kraken's
fixed public OHLC host with allowlisted pairs (`BTC`, `ETH`); `DemoSource` reuses
`synthetic_day()`; `KiteLiveSource` is disabled and its `fetch` only explains
that C4 will add streaming. See [data-sources.md](data-sources.md).

**Normalised data.** A `Candle` carries a true UTC epoch second plus the original
values; the browser formats IST with `Intl.DateTimeFormat` for the axis, tooltip
and freshness line. 3-minute candles are resampled from 1-minute ones.
Heikin-Ashi is a tested pure function; all indicators come from
`kite_alerts.indicators` with the saved `StrategyParams`, using
`indicator_frame()` so the chart values are the same series the rules see.

**Resilience.** Every adapter uses `ProviderCache`: 5–10 s TTL (Kraken ~1–1.5 s),
one in-flight fetch per key with a lock, exponential backoff with jitter, and a
circuit breaker after three failures. On failure the last candles remain on the
page with a visible "stale since" mark and a retry message; no provider URL or
body is logged or returned. `ChartService` validates source/symbol/interval/limit
before any fetch.

**Honesty.** `/api/chart/candles` returns `badge`, `source_label`,
`freshness.latest_ist`, `age_seconds`, `age_text`, `checked_at`, the measured
`delay_seconds`, `stale`/`stale_since`, `message` and the market clock state.
The page has a permanent high-contrast badge, and PNG exports burn the badge and
its plain-language meaning into the image. The signal meter shows the five rules
as status lights with a summary such as "3 of 5 agree (bullish), waiting for
MACD" and the exact label "Informational, based on <source> data. No alerts are
sent from this source." ORB is unavailable for crypto; OI is unavailable for both
free sources.

**Watchlist.** Configured `SYMBOLS` are mapped per source; user-added NSE symbols
are stored atomically in `data/chart_watchlist.json` (git-ignored), capped at 10.
Rows show last price, change %, sparkline and freshness; the click target loads
the chart. The endpoint caches rows for 15 seconds; provider caches do the rest.

**No alert path.** `chart/` never imports the engine, notifier, `AlertStore` or
history. Tests statically assert those imports are absent and call every chart
route with alert sending patched to fail, then assert the alert store counts are
unchanged.

## Guided notifications

Settings renders Telegram and Slack guides from field metadata, with
`@BotFather` instructions and channel checkboxes (console disabled and always
on). `POST /api/notifications/telegram/chat-id` reads
`https://api.telegram.org/bot<token>/getUpdates` server-side only, takes the
newest chat id from `message`, `edited_message`, `channel_post` or
`my_chat_member`, and fills the field in the browser. It never returns the bot
token. 401, 409 (webhook set), 429, other HTTP errors, an `ok: false` payload and
network failures each map to a distinct plain message. The token, chat id and
webhook are saved through the same settings store, and "Send test alert" reuses
the C1 test-alert route with per-channel results.

## Security invariants (do not regress)

1. `--host` must be loopback; `validate_bind_host` runs before the app is built.
2. Every request must carry a loopback `Host` header (`SecurityMiddleware`).
3. Every state-changing route calls `require_same_origin` (Origin and
   `Sec-Fetch-Site`); there is no CORS middleware.
4. The CSP is `default-src 'none'` with `script-src 'self'`; no inline script, no
   CDN, no external font, no telemetry. The vendored chart library's injected
   stylesheet is allowed by SHA-256 hash; inline style *attributes* are allowed
   for its canvases.
5. All browser text is written with `textContent`. No UI module uses
   `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write`; a test
   scans every JS/HTML asset.
6. No endpoint returns a credential. The secrets test sends dummy API key,
   secret, access token, request token, Telegram token and Slack webhook through
   every new route (including error paths) and fails on any echoed value.
7. Secrets are never logged: log lines carry only an error class or a redacted
   message, never a token, secret, request token or webhook URL.
8. The only Kite SDK methods are `login_url`, `generate_session`, `profile`,
   `ltp`, `invalidate_access_token` and `set_access_token`. A test scans
   `src/kite_alerts/ui/` for any other SDK call and for order/trade/position
   names; `tests/test_alerts_only.py` extends that scan to every new `.py`,
   `.js`, `.html` and `.css` asset.
9. Settings writes are allowlisted, validated, atomic, backup-preserving and
   rate-limited; the allowlist and the model's own readers are the only way a
   value reaches `.env`.
10. The history database stays `mode=ro` + `PRAGMA query_only`. Practice and
    "Test my settings" never write to it.

## Tests and verification

No test reaches the network. `tests/unit/ui_support_c2.py` provides a temporary
`.env`, `FakeKite`/`FakeKiteFactory`, `FakeTelegram`/`FakeResponse` and a context
builder; `FakeMonotonic` controls the ten-minute state window and the caches.

C3 tests use `tests/unit/ui_support_c3.py` (fixed fake candles, a fake source and
a frozen clock). They cover Heikin-Ashi known values, 3-minute resampling, symbol
mapping (including NFO refusal), indicator parity with the bot functions,
freshness/age and stale states, cache TTL/backoff/breaker/coalescing, symbol and
parameter validation/SSRF, disabled Kite, crypto parsing and errors, watchlist
persistence, holiday/weekend/open/pre-open clock states, static badges and picker
disabling, and alert isolation.

Covered: successful connect; missing/wrong/expired state; missing/cancelled
attempt; manual fallback; exchange and network errors; expired/unreachable
session; disconnect; every data-plan outcome and the cache; settings metadata,
save, validation, unknown keys, corrupted input, interrupted replace, backup
pruning, rate limit, secret clearing and redaction; chat-id detection and its
error codes; "Test my settings" using saved parameters; the sidebar/routing
static checks; credential non-leakage; the Kite allowlist; gitignore.

`kite-alerts demo` still matches `demo_output.txt` exactly (line endings aside on
Windows checkouts). The full suite runs under the 85% coverage gate plus ruff,
black, mypy, import-integrity and the alerts-only scan.

Explicitly **unverified** until someone uses real accounts: whether the Kite
console accepts a loopback redirect URL, how a real plan-less account reports the
missing market-data permission, real Telegram delivery, the owner's browser
rendering and Windows firewall behaviour. These are documented rather than
guessed.

## Explicitly out of scope for C3

Kite live websocket streaming (C4); CSV replay; starting or stopping the bot
from the page; the journal. Any order, position, holding or trade functionality
is out of scope forever, as is a shared Kite API key.
