# Dashboard design (C1 shipped, C2/C3 planned)

C1 ships a local, read-only dashboard with an offline demo player. This note
records the shape it settled on, so C2 (settings + Zerodha login) and C3 (CSV
replay + start/stop with logs) can be added without moving C1.

## Layering

```
kite_alerts/ui/
├── app.py            FastAPI factory, UiContext, JSON routes
├── security.py       loopback bind check, Host validation, CSP, origin check
├── history.py        read-only SQLite reader (mode=ro + PRAGMA query_only)
├── status.py         probes the bot's existing /health endpoint
├── demo_player.py    the synthetic day through the real Engine
├── series.py         EMA/RSI/MACD/ORB series from the bot's own indicators
├── test_alert.py     rate-limited TEST delivery with per-channel results
├── server.py         validate host/port, build the app, run uvicorn
└── static/           index.html, styles.css, app.js, vendored chart library
```

`kite_alerts.ui.__init__` imports nothing heavy, and `cli/main.py` imports
`ui.server` lazily. Installing the bot without the `ui` extra therefore never
touches FastAPI or uvicorn.

Every route gets its dependencies from one explicitly constructed `UiContext`,
which is how the tests inject a fake probe, a fake notifier factory and a
temporary database. C2/C3 keep that pattern: add services to the context, not
globals.

## API surface

| Route | Method | C1 | C2 | C3 |
|---|---|---|---|---|
| `/api/status` | GET | bot health + today's counts | + settings warnings | + run state |
| `/api/alerts/recent` | GET | latest alerts | — | — |
| `/api/alerts/history` | GET | filters + pagination | — | — |
| `/api/demo/timeline` | GET | full demo timeline | — | — |
| `/api/demo/stream` | GET | NDJSON replay | — | — |
| `/api/test-alert` | POST | rate-limited TEST | — | — |
| `/api/about` | GET | version, mode, checklist | + login state | — |
| `/api/settings` | GET | — | field metadata (labels, types, defaults, **no secret values**) | — |
| `/api/settings` | PUT | — | validate + atomic write | — |
| `/api/kite/login` | POST | — | start official login redirect | — |
| `/api/kite/callback` | GET | — | loopback callback: store the daily token | — |
| `/api/kite/session` | GET | — | `Connected as <name>, expires ~06:00` | — |
| `/api/replay` | POST | — | — | CSV replay with forward returns |
| `/api/run` | POST | — | — | start the bot subprocess |
| `/api/run` | DELETE | — | — | stop it |
| `/api/run/logs` | GET | — | — | SSE/NDJSON log tail |

Routes stay in `app.py` while they are small; once C2 adds settings + login,
split them into `ui/settings_routes.py` and `ui/login_routes.py` with an
`APIRouter` each, included by `create_app`. The security middleware, context
object and static assets are unchanged by that split.

## C2: settings form and "Connect to Zerodha"

**Field metadata from the pydantic models.** A new `ui/settings_schema.py`
walks `Settings` / `StrategyParams` / `NotifySettings` / `SessionSettings` /
`ResilienceSettings` / `ObservabilitySettings` field metadata (`title`,
`description`, `ge`/`le`, enum choices) and returns ordered groups. Add a
`Field(..., description="...")` (or a small plain-language label map keyed by
field name) rather than a parallel hand-written schema. The UI renders inputs
from that metadata, so a new pydantic field appears without a JS change.

**Validation.** `PUT /api/settings` applies the values to a copy, runs
`Settings(...)`, and returns per-field errors. Pydantic's error locations map
1:1 to form fields; nothing is written unless the whole model validates.

**Atomic write with backup.** New `ui/settings_store.py`:
1. read the current `.env` (comments preserved);
2. replace only the changed keys, quote values containing `#`, spaces or `=`;
3. write `.env.tmp` in the same directory, `fsync`, then `os.replace()`;
4. first copy the old file to `.env.bak-<UTC timestamp>`;
5. never log or return a secret value.

YAML per-symbol overrides use the same write-temp-then-replace helper.

**Secret fields are write-only.** The GET schema marks a secret field as
`{"secret": true, "is_set": true}`; it never returns the value. The PUT accepts
`null`/omitted to mean "leave unchanged", and an explicit `""` to clear. The
secrets-never-leaked test from C1 extends to these routes automatically because
it calls every registered route.

**Login flow.** New `ui/login_routes.py` reuses `cli/login_cmd.py`:
- `POST /api/kite/login` calls the existing `KiteConnect.login_url()` and returns
  the URL; the browser is sent there with a normal redirect, so the user types
  their password and TOTP **on Zerodha's site**. The app never sees them.
- The user configures the loopback callback in their Kite app; the callback route
  receives `request_token`, then calls `login_cmd.extract_request_token()` and
  `login_cmd.write_access_token()` — the same functions the CLI uses. No duplicate
  exchange logic.
- `GET /api/kite/session` calls `KiteConnect.profile()` if a token exists and
  returns only `{name, connected, expires_note}`. Tokens are never returned.
- Fallback: a "paste the request_token" form posts to the same callback handler.

Nothing in C2 stores a password, automates a browser, or bypasses Zerodha's page.

## C3: CSV replay and start/stop with logs

**CSV replay.** `POST /api/replay` accepts an uploaded CSV, runs it through the
existing `backtest/replay.py` functions (`load_csv`, `replay_frame`,
`build_result`), and returns the same `ANALYSIS ONLY` report plus forward returns
as JSON. Uploads are size-capped, parsed in memory, and never written into the
repository. Add a chart layer on top of the existing `series.build_chart` by
passing the replay frame.

**Start/stop.** `ui/run_control.py` owns a single supervised subprocess
(`python -m kite_alerts run`), a PID file under `logs/`, and a bounded ring
buffer of its output. It:
- refuses to start if the health endpoint already answers;
- captures stdout/stderr with the existing redaction filter (never the webhook
  URL or tokens) and streams it over `GET /api/run/logs` as NDJSON;
- stops with SIGTERM (Ctrl-Break / `taskkill` on Windows), then waits, then
  reports; it never touches positions, because the bot has no order path;
- is reachable only from the loopback dashboard, behind the same Host and
  same-origin checks as `/api/test-alert`.

## Security invariants (do not regress in C2/C3)

1. `--host` must be loopback; `validate_bind_host` is called before the app is
   built.
2. Every request must carry a loopback `Host` header (`SecurityMiddleware`).
3. State-changing routes call `require_same_origin(request)`.
4. No CORS middleware is added, ever. The CSP blocks inline scripts and
   arbitrary `<style>` blocks. The vendored chart library needs inline style
   *attributes* (`style-src-attr 'unsafe-inline'`) and injects one static
   stylesheet, which is allowed by SHA-256 hash instead of `'unsafe-inline'`.
   Recompute that hash when upgrading the chart library.
5. All browser text is written with `textContent`; `innerHTML` does not appear in
   `static/app.js`.
6. No route returns a credential; the C1 test enumerates every route and fails if
   a dummy secret appears in any response body.
7. The UI database handle is always `mode=ro` + `PRAGMA query_only`.
8. `tests/test_alerts_only.py` scans the UI's `.py`, `.js`, `.html` and `.css`
   for order-API names and order endpoint paths.
9. No runtime request leaves the machine: no CDN, no external font, no telemetry.
   The vendored chart library and its licence live in `static/vendor/`.

## Explicitly out of scope for C1

Starting the live feed, editing settings, Zerodha login, CSV replay, any order
or position functionality, and any write to the alert database.
