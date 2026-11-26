# IMPLEMENTATION.md — audit, concurrency design and phased plan

Status: **Stage 1 deliverable** (documents only; no product code changed).
Branch: `docs/implementation-plan`, created from `origin/main` = `233ed87`.
Verified on Windows 11, Python **3.14.7** (the owner's runtime), pandas 2.3.3,
numpy 2.4.6, `kiteconnect` from PyPI, repo installed with `pip install -e ".[dev]"`.

Every claim below carries a `file:line` or a command output. Anything I could not
run is listed as UNVERIFIED in section 8 rather than asserted.

**This document is also the live status register**: each implementation phase
updates the snapshot table below (§0) and the findings it closes. Sections 2–9
describe the audit *as it was written* against `233ed87`; the snapshot table and
the N findings (§2.N) describe what actually shipped, branch by branch.

---

## 0. Status snapshot (updated by every phase)

Verified on branch `fix/p5-refactor` (base `main` = `2a875cf`), Python
3.14.7, 926 tests collected (919 run + 7 `e2e_ui` browser tests), full suite green,
coverage **92.76%** (floor 85%), `ruff check` + `black --check` clean,
`mypy src/kite_alerts` clean (83 source files), `tests/test_alerts_only.py` green,
19 Node JS tests green, and 7 Playwright browser tests green.

| Item | Status | Where |
|---|---|---|
| F1 feed never started | **DONE** (P0, PR #19) | `ui/feed_controller.py` |
| F2 subscribe before on_connect | **DONE** (P0, PR #19) | `ui/live_feed.py` |
| F3 official holiday list | **PARTIAL** — parser + script shipped; NSE blocks scripted fetches, owner must run `scripts/refresh_holidays.py` once (N4) | `config/holidays.yaml` |
| F4 market clock | **DONE** (P1, PR #20) | `core/calendar.py`, `ui/status.py` |
| F5 one availability truth | **DONE** (P0.1) | `ui/live_feed.py availability()`, read by every surface |
| F6 mutations without Origin | **DONE** (P2, PR #22 + `fix/p2-closeout`) | `ui/security.py`, README |
| F7 .env.tmp in messages | not reproduced (regression test added, P2) | §2.7 |
| F8 nanosecond UserWarning | **DONE** (P2, PR #22) | `core/clock.py` |
| F9 release workflow | **DONE** (P3, PR #27) | `.github/workflows/release.yml` |
| F10 CI misses 3.14 | **DONE** (P3, PR #27) | `.github/workflows/ci.yml` |
| F11 product path under-tested | **DONE** (P4, PR #28) | `tests/cli/test_run_faults.py`, `tests/cli/test_main_faults.py`, `tests/engine/test_worker_faults.py` |
| F12 files too large | **PARTIAL** — live-feed types + bootstrap extracted (P5); other file splits remain | `ui/live_feed_types.py`, `ui/live_feed_bootstrap.py` |
| F13 documentation drift | **DONE** (P6, PR #31) | `AGENTS.md`, `docs/ROADMAP.md`, `docs/live-feed.md`, `tests/unit/test_docs_truth.py` |
| F14 relative paths | **DONE** (P2, PR #22) | `config/settings.py`, About page |
| F15 no frontend tests | **DONE** (P4, PR #28) | `tests/js/*.test.mjs`, `tests/e2e_ui/test_smoke.py`, CI `ui-js`/`ui-browser` |
| F16 delayed/crypto vs real providers | **DONE** (P3, PR #27; live run still UNVERIFIED) | `scripts/smoke_sources.py`, `docs/data-sources.md` |
| F17 SSE blocks the event loop | **DONE** (fix on `main`; lifecycle tests in P0.1) | `ui/app.py` stream |
| F18 notifier queue unbounded | **DONE** (P2, PR #22 + `fix/p2-closeout`) | `notifiers/hub.py` |
| F19 forming candle from wall clock | **DONE** (P0, PR #19) | `ui/live_feed.py` |
| F20 stop() poisons the feed | **DONE** (P0, PR #19) | `ui/live_feed.py` |
| F21 watchlist write race | **DONE** (P2b, PR #26) | `ui/chart/service.py` |
| F22 unsynchronised shared dicts | **DONE** (P2b, PR #26; `_registry` was already locked, finding corrected) | `ui/live_feed.py` |
| F23 golden-file line endings | **DONE** (P3, PR #27) | `.gitattributes` |
| F24 backoff constants unused | **DONE** (P0, PR #19) | `ui/live_feed.py` |
| F25 calendar cache by identity | **DONE** (P3, PR #27) | `ui/chart/service.py` |
| F26 `.env` read-modify-write race | **DONE** (P2, PR #22) | `config/env_file.py` |
| F27 late `on_connect` races `stop()` | **DONE** (P4, PR #28) | `ui/live_feed.py`, §2.27 |
| N1 surfaces lie after token expiry | **DONE** (P0.1) | §2.N |
| N2 data-sources 60 s lag | **DONE** (P0.1) | §2.N |
| N3 SSE loop untested | **DONE** (P0.1; tests pass on `main` by design) | §2.N |
| N4 holiday list empty on this machine | **RECORDED** — manual owner step | §2.N |
| N5 phase assignments for leftover findings | **RECORDED** | §2.N |

Phase assignment record (N5, per owner instruction): F18 → **P2** (merged);
F21, F22 → **P2b** (PR #26); F23, F25 → **P3** (PR #27).

---

## 1. Scope, non-goals, how verification was done

### 1.1 Scope (P0–P6)

Make the **live Kite feed actually work for a real connected user**, then make the
calendar, the security posture, the release pipeline, the tests and the docs
honest. One phase per branch, each with a reproducing test that fails first, all
CI jobs green, nothing merged.

### 1.2 Non-goals

- **No order / trade / position / holdings / GTT code, ever.** `tests/test_alerts_only.py`
  scans Python *and* `.js/.html/.css` and must stay green (it does today).
- **No change to signal rules or evaluator semantics.** If a real bug existed
  there it would be documented, not fixed. I found none: the five rules
  (`strategy/rules/*.py`), the evaluator and `Engine` are unchanged by anything
  proposed here.
- **No secrets** in git, logs, HTTP responses or alerts; bring-your-own-key only.
- **No paid service, no CDN, no telemetry.** The chart library stays vendored.
- **Delayed/crypto/simulated data must never reach the notifier, alert store or
  alert history.** True today; P0 must not break it.
- **Docker is not coming back.** Packaging is PyInstaller + `scripts/`.

### 1.3 Environment setup

The checkout had **no git remote**, so "latest main" could not be fetched normally:

```
git remote add origin https://github.com/AmritBakshi2003/Zerodha_Python_Alert_Bot.git
git fetch origin main      # origin/main = 233ed877cd3483db4697df203d3a0ea0b5450540
git switch -c docs/implementation-plan origin/main
py -3 -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Uncommitted C1-era work from a previous session was preserved with
`git stash push -u -m "C1 dashboard WIP ..."` before switching; it is unrelated to
the current `main`, which already contains a much newer `ui/`.

### 1.4 Commands run and actual results

| # | Command | Result |
|---|---|---|
| 1 | `python -m pytest --cov=kite_alerts -q` | exit 0; `Total coverage: 90.52%`; 6866 stmts, 531 missed, 1622 branches, 226 partial |
| 2 | `pytest --collect-only` (summed) | **725 tests collected** (ROADMAP's number is accurate) |
| 3 | `ruff check src tests` | `All checks passed!` |
| 4 | `ruff format --check src tests` | `133 files already formatted` |
| 5 | `black --check src tests` | `132 files would be left unchanged` |
| 6 | `mypy src/kite_alerts` | `Success: no issues found in 81 source files` |
| 7 | `python scripts/check_demo_golden.py` | `OK: ... matches demo_output.txt (5 lines)` |
| 8 | raw `diff demo_output.txt <(python -m kite_alerts.demo)` | **differs by line endings only** (F23): blob is LF, working tree CRLF via `core.autocrlf=true`, repo has no `.gitattributes`. Content identical, official script passes |
| 9 | `/tmp/repro_f1.py` | reproduces F1 (§2.1) |
| 10 | `/tmp/repro_f2.py` | reproduces F2 (§2.2) |
| 11 | `grep -rn "\.env\.tmp" src/kite_alerts` | **no matches** → F7 not reproducible here (§2.7) |
| 12 | `grep -rn "STATE_NO_PLAN" src/kite_alerts` | definition + two reads only → state unreachable (F5) |
| 13 | inspect `.venv/Lib/site-packages/kiteconnect/ticker.py:568-585` | `subscribe()` sends on `self.ws`, calls `self._close(...)` and re-raises on failure — the mechanism behind F2 |
| 14 | coverage rows | `cli/run.py 68%`, `cli/main.py 75%`, `engine/worker.py 80%` (F11) |

`tests/test_alerts_only.py` is green, including the frontend scan. No order-side
identifier exists anywhere in the tree.

### 1.5 What this audit could not do

**No real Kite session was used.** Each live-data conclusion is (a) provable from
our code, (b) provable with a fake ticker that behaves like `kiteconnect` 5.x, or
(c) quoted from official documentation with a citation. Section 8 is the honest
remainder.
---

## 2. Findings register

Severity: **HIGH** = the advertised feature does not work for a real user;
**MED** = works but is unsafe, fragile or misleading in a way a user hits;
**LOW** = correctness, hygiene or documentation drift.

Totals: **2 HIGH, 7 MED, 17 LOW = 26 findings** (F7 is *unconfirmed* and says so).

### 2.1 F1 — HIGH — the live Kite chart never starts for a real user

**Evidence.** `ui/live_feed.py:536` defines `run_bootstrap()`, `:388` defines
`start()`. **Nothing in `src/kite_alerts/ui/` calls either.** A tree-wide grep finds
`feed.start(` / `run_bootstrap(` / `.subscribe(` callers only in `cli/run.py` and
`engine/worker.py` (the *bot's* feed) and `ui/simulator.py:100`. The dashboard
constructs the service at `ui/app.py:190-213` and only calls `feed.stop()` on
shutdown (`app.py:356`, `server.py:131`).

Reproduction — connected user, plan probe succeeding, no simulator
(`/tmp/repro_f1.py`, real `build_context`/`create_app` + `TestClient`):

```
GET /api/kite/session: HTTP 200   {"status":"connected","connected":true,"name":"Repro Trader",...}
GET /api/data-sources            kite_live: available=true, enabled=true, badge="LIVE",
                                 notice="Live data available from your Kite account."
GET /api/chart/candles?source=kite_live
                                 HTTP 422 {"code":"source_disabled",
                                 "message":"Live Kite feed arrives in a later update (C4). ..."}
feed status: state='disconnected', subscribed=0, last_tick_time='', latency_ms=None
tickers created: 0 | subscribed tokens: [] | bootstrap cache: {}
```

**Root cause.** Three missing links, any one fatal: (1) nothing calls `start()`
after a successful connect; (2) nothing resolves a chart symbol to an
`instrument_token` (`TokenRegistry.register` is reached only from `subscribe()`,
which only the simulator calls); (3) `run_bootstrap()`, the only producer of
`_bootstrap_data` that `KiteLiveSource.fetch()` reads (`chart/sources.py:386-449`),
is never invoked.

**User impact.** A paying customer connects Zerodha, the UI says **LIVE**, and the
live chart refuses to draw with a message about a future release. Worst defect in
the product.

**Fix design (P0).** Add `FeedController.ensure_started()`: resolve symbols→tokens
via the existing instrument cache, `subscribe([(symbol, token, is_fut), ...])`,
`start(api_key, access_token)`, then `run_bootstrap()` per symbol on a bounded pool
(≤2 concurrent). Call it from `KiteConnectService.finish()/reload()` and from the
chart symbol-resolution path. Idempotent while `CONNECTING/LIVE`; token change
rebuilds the ticker (§3e). `ChartService.candles()` must ask the state machine
(F5) instead of answering 422 with a hard-coded C4 string.

**Tests that prove it.** `tests/ui/test_live_feed_start.py`:
`test_connecting_starts_the_ticker_and_subscribes_resolved_tokens` (fails on
`main`: zero ticker creations), `test_chart_symbol_selection_starts_feed`,
`test_bootstrap_populates_candles_for_the_chart`,
`test_fake_ticker_ticks_reach_the_chart` (FakeTicker→on_ticks→builder→candles).

**Acceptance.** Fake Kite client, no simulator: after login `/api/data-sources`
says available, candles returns bootstrap candles, `/api/chart/stream` emits tick
events, `status().subscribed` equals resolved symbols, no C4 string on the path.

**Risk / rollback.** Touching the connect flow risks the practice replay; keep
`ensure_started()` a no-op without settings/tokens and run the full suite.
Rollback = revert the P0 commit.

### 2.2 F2 — HIGH — subscribe() runs before the socket is open

**Evidence.** `ui/live_feed.py:589-598`:

```python
def _connect() -> None:
    try:
        ticker.connect(threaded=True)
        if tokens_quote: ticker.subscribe(tokens_quote); ticker.set_mode("quote", tokens_quote)
        if tokens_full:  ticker.subscribe(tokens_full);  ticker.set_mode("full", tokens_full)
    except Exception: log.warning("live feed: connect failed (%s)", type(exc).__name__)
```

`connect(threaded=True)` returns after starting a thread; the socket is not open.
`kiteconnect`'s `subscribe()` writes to `self.ws`, calls `self._close(...)` and
**re-raises** on failure (`kiteconnect/ticker.py:568-585`) — so the failed subscribe
also closes the socket. The bot's own feed does it right: `data/feed.py:_on_connect`
→ `_subscribe()`. The public `subscribe()` (`live_feed.py:410-450`) also calls the
ticker with no connection check and registers tokens *before* the exchange accepts
them, so `status().subscribed` counts intentions.

Reproduction (`/tmp/repro_f2.py`, fake ticker that raises exactly like 5.x):

```
current design (subscribe immediately after connect(threaded=True)):
  state='live' reason='' subscribed_count=1 exchange_subscribed=[] socket_closed=True
```

**Root cause.** Subscription is sequenced against `connect()` (thread start) instead
of `on_connect` (handshake), and the failure is swallowed.

**User impact.** Green **LIVE** badge, N "subscribed" symbols, no ticks ever — and
the socket is closed by the failed call, so the lie is stable.

**Fix design (P0).** `subscribe()` only enqueues into `_pending_quote/_pending_full`;
flush inside `_on_connect()` (and `_on_reconnect`) as `data/feed.py` does. On flush
failure go to `STATE_DISCONNECTED` with a plain-language reason and let the
supervisor retry. `status().subscribed` reports flushed tokens only.

**Tests.** `tests/ui/test_live_feed_subscribe.py` with a fake that raises until
`on_connect` and records call order:
`test_subscribe_is_never_called_before_on_connect` (fails on `main` with
`RuntimeError` + `closed=True`), `test_pending_subscriptions_flush_after_reconnect`,
`test_failed_flush_marks_disconnected_not_live`, `test_status_counts_flushed_tokens_only`.

**Acceptance.** The fake sees `connect → on_connect → subscribe` on every path
(login, chart pick, reconnect, simulator); a subscribe failure can never leave the
state at `live`.

**Risk / rollback.** Local to `live_feed.py`; revert the commit if needed.
### 2.3 F3 — MED — holidays are empty, so "holiday" never happens

**Evidence.** `config/holidays.yaml` ends with `holidays: []`. `describe()` can
therefore never return `"holiday"` (`core/calendar.py:116-124`) and
`holidays_stale_days()` returns `None` for an empty list (`:138-143`), so the
staleness warning is silent too. `docs/live-feed.md` §5 promises a "Market closed"
badge "on a holiday".

**User impact.** On a trading holiday the bot stays connected, sees no ticks, and
the watchdog reports staleness instead of "closed" — noise exactly when nothing is
wrong.

**Fix design (P1).** Ship the **official** NSE list only if verifiable from
nseindia.com; otherwise add `scripts/refresh_holidays.py` (fetch official page →
write YAML), warn loudly at startup when the list is empty or older than the
current year, and show it on the About page. **Never invent dates.**

**Tests.** `test_calendar_reports_holiday_for_configured_date`,
`test_empty_holiday_file_warns_at_startup`, `test_stale_holiday_list_warns`,
`test_refresh_helper_writes_expected_yaml_shape` (fixture HTML, no network).

**Acceptance.** A configured holiday gives `describe()=="holiday"`,
`in_session==False`, no alerts, one startup warning naming file and year.

**Risk / rollback.** A wrong date would silence a real session — hence official
sourcing, plus the watchdog still flagging an unexplained closed market.

### 2.4 F4 — LOW — the market clock mislabels most of the day

**Evidence.** `core/calendar.py:120-124`:

```python
if moment.time() < self.window.open_time: return "pre-open"
if moment.time() > self.window.close_time: return "post-close"
return "open"
```

01:40 IST reports `pre-open`; the real pre-open is 09:00–09:15. Evenings report
`post-close` instead of "closed, opens 09:15". No special-session concept exists
(grep: no `muhurat`, no `special session`).

**User impact.** The dashboard's session text is wrong for ~18 hours a day, and the
UI's ORB/alert explanations inherit it.

**Fix design (P1).** One `describe()` returning: `closed-before-open` (with
`opens 09:15`), `pre-open` (09:00–09:15; sub-phases 09:00–09:10 entry,
09:10–09:12 matching, 09:12–09:15 buffer), `open` (09:15–15:30), `closing`
(15:30–16:00, context only), `weekend`, `holiday`, `special-session` (configured
exceptions: muhurat, special Saturday). Tradable window stays 09:15–15:30.

**Tests.** A property test over every minute of a day (exhaustive coverage, monotone
transitions) plus cases 01:40, 08:59, 09:00, 09:12, 09:15, 15:29, 15:30, 15:31,
16:01, Saturday, holiday, configured special session.

**Acceptance.** Every minute maps to one documented state; special sessions come
from config; UI strings derive from this single function.

**Risk / rollback.** The tradable predicate is unchanged, so no signal moves; revert
the mapping.

### 2.5 F5 — LOW — three disagreeing sources of truth, and C4 strings on live paths

**Evidence.**
- Stale strings: `ui/kite_connect.py:443,456,471`; `ui/chart/sources.py:388`;
  `ui/chart/service.py:229,339`; `ui/static/chart.js:26`.
- Three answers for "is kite_live available?": `data_sources()` uses the **plan
  probe** (`kite_connect.py:410-490` → `available:true, enabled:true, badge "LIVE"`);
  `KiteLiveSource.enabled` uses the **feed state** (`chart/sources.py:330-340` →
  `False` while disconnected); `candles()` raises on `not enabled` with the C4 text
  (`chart/service.py:334-343`); and the frontend **hard-codes**
  `source.enabled = source.id !== "kite_live"` (`static/chart.js:365`), disabling a
  working feed in the picker.
- `STATE_NO_PLAN` is defined (`live_feed.py:72`) and read
  (`chart/sources.py:332,340`) but **never assigned** → unreachable state.

**User impact.** Picker, status card and candles endpoint can disagree in the same
session — exactly the confusion F1 produces.

**Fix design (P0).** One state enum and one resolver
`feed.availability() → (available, enabled, badge, notice, reason)` consumed by
`data_sources`, `source_info`, `candles`, `/api/chart/stream` and the JS picker (the
JS must stop overriding `enabled`). Delete every C4 string. Either assign
`STATE_NO_PLAN` from the plan probe or remove it.

**Tests.** `test_all_surfaces_agree_for_each_feed_state` (parametrised over
disconnected/connecting/live/stale/token_expired/no_plan/simulated),
`test_frontend_does_not_hardcode_source_availability`,
`test_no_coming_soon_strings_remain` (grep-style).

**Acceptance.** For every state the three surfaces agree; grep for
`later update (C4)` returns nothing.

**Risk / rollback.** Small frontend change; revert the commit.

### 2.6 F6 — LOW — mutations accepted without an Origin header

**Evidence.** `ui/security.py:130-145`:

```python
origin = request.headers.get("origin")
if origin: ...compare with Host...
site = (request.headers.get("sec-fetch-site") or "").lower()
return not site or site in ("same-origin", "none")
```

The docstring states a missing `Origin` is accepted deliberately. A cross-site form
POST from a modern browser does send `Origin`, so the practical risk is low, but any
local non-browser client (or a legacy browser) can mutate state, and
`Sec-Fetch-Site` is optional too.

**User impact.** Local-only; mitigated by loopback bind + `Host` check; still below
the project's own stated bar.

**Fix design (P2).** Per-session CSRF token: `GET /` issues an
`HttpOnly; SameSite=Strict` cookie and exposes the token to the page; every
`PUT/POST/DELETE` requires `X-Kite-Alerts-CSRF` matching the cookie. Keep `Host` and
`Origin` checks. Document the browser-vs-`curl` trade-off with a supported way for
`curl` users to obtain the token.

**Tests.** `test_mutation_without_csrf_token_is_403`,
`test_mutation_with_token_but_foreign_origin_is_403`,
`test_csrf_token_is_stable_within_a_session`, `test_reads_do_not_require_the_token`.

**Acceptance.** All mutations require the token; documented `curl` path works; the
token is not exposed where it is not needed.

**Risk / rollback.** A CSRF miss breaks the UI; mitigate with the Playwright smoke
test from F15 covering settings-save and test-alert. Revert on failure.

**Landed (P2).** The token is issued by `GET /` (meta tag) and `GET /api/about`
(`csrf_token` in JSON) as an `HttpOnly; SameSite=Strict` cookie; `require_csrf_token`
runs beside `require_same_origin` on every mutation. P2's first pass left two gaps,
closed on `fix/p2-closeout`:

* the `curl` handshake was only in the PR body, so a user reading the repo could not
  script the dashboard. README now documents it under "Scripting the dashboard
  (curl)", pinned by `test_curl_handshake_is_documented` (fails on `bf7fb25`);
* token resolution was duplicated inline in `index()` and `about()` (with subtly
  different minimum-length checks) and `csrf_token_for` was dead. Both routes now
  call `resolve_csrf()`/`set_csrf_cookie()`, and the unused import is gone.

### 2.7 F7 — LOW — ".env.tmp" in settings warnings — **NOT REPRODUCED**

**Evidence.** `grep -rn "\.env\.tmp" src/kite_alerts` → **no matches**. The temp path
is built dynamically (`ui/settings_store.py:236`,
`env_file.with_name(name + ".tmp")`) and every user-facing `save()` message is
static ("Some values are not valid, so nothing was saved.", "The settings file could
not be replaced. The original is unchanged."). `load_settings(temp)` errors are keyed
by env var, not by path.

**Conclusion.** The described defect does not exist on `233ed87`; it may have been
observed on an older revision. **No fix planned.** P2 adds a regression test that no
settings message contains `.tmp`, so it cannot reappear.

### 2.8 F8 — LOW — a UserWarning escapes from clock conversion

**Evidence.** `core/clock.py:42` (`resolved = value.to_pydatetime()`); reproduced:

```
python -W error::UserWarning -c "...to_ist_ts(pd.Timestamp('2026-01-01 09:15:00.000000001', tz='Asia/Kolkata'))"
UserWarning: Discarding nonzero nanoseconds in conversion.
```

**User impact.** Noisy logs; callers that promote warnings to errors fail.

**Fix design (P2).** Drop sub-microsecond precision before conversion
(`Timestamp.as_unit("us")` / `.floor("us")`, or build the datetime from
`ts.timestamp()`); identical behaviour at minute resolution. Test under
`-W error::UserWarning`.

**Acceptance.** No warning; suite green with `UserWarning` promoted to error for
`kite_alerts.*`.

**Risk / rollback.** One-line change; trivially revertible.
### 2.9 F9 — MED — the release workflow has never been exercised

**Evidence.** `.github/workflows/release.yml` uses `actions/upload-artifact@v7`
(lines 65, 98) and `actions/download-artifact@v8` (117, 122) and has **no
`workflow_dispatch`** trigger, so the only way to learn whether artifacts still pass
between jobs is to cut a release.

**User impact.** A broken release is discovered at release time, possibly after a tag
exists.

**Fix design (P3).** Add `workflow_dispatch` with `dry_run: true` (default) that runs
every build/pass-artifact job and skips any publish (`gh release create`, PyPI). Keep
the tag path unchanged. Guard every publish step with an explicit `if:` on `dry_run`.

**Tests.** `tests/test_workflows.py` (YAML shape): dry-run input exists; publish steps
are guarded; artifact action versions match between upload/download sites. Then one
manual dry-run run, linked in the PR.

**Acceptance.** Manual dry-run green end to end; publish provably skipped.

**Risk / rollback.** Workflow-only; revert the file.

**Landed (P3).** `release.yml` now accepts `workflow_dispatch` with a `dry_run`
boolean (default `true`), and the only publish step (`gh release create`) is
guarded with `if: github.event_name == 'push'`. A dispatch therefore builds the
sdist/wheel, passes the `dist` artifact to the publish job, recomputes the
checksums and stops; a `v*` tag keeps the old behaviour. `tests/test_workflows.py`
pins the triggers, the guard on every publish command, and one version per
artifact action. **The dry run itself has not been executed** (it needs a push
to the repo); it is in the PR's UNVERIFIED list until the first manual run.

### 2.10 F10 — LOW — CI never tests the owner's Python

**Evidence.** `.github/workflows/ci.yml:65` →
`python-version: ["3.10", "3.11", "3.12"]`; other jobs pin `3.12`. The owner runs
**3.14.7**, verified in this audit.

**User impact.** The runtime that matters most to the customer is untested.

**Fix design (P3).** Add 3.13 and 3.14 with `continue-on-error: true` (and an
`experimental: true` matrix flag) so they report without blocking; promote to
blocking once green twice. Update classifiers and README.

**Tests.** The matrix itself; the PR links the 3.13/3.14 job logs.

**Acceptance.** CI covers 3.10–3.14; 3.13/3.14 visible and non-blocking; README's
"target Python" matches reality.

**Risk / rollback.** None (non-blocking); revert the matrix lines.

**Landed (P3).** The `test` job now runs an explicit matrix of 3.10-3.14 with an
`experimental` flag, and the job's `continue-on-error: ${{ matrix.experimental }}`
keeps 3.13/3.14 visible without blocking. Classifiers gained 3.13/3.14 and the
README says 3.10-3.14. `tests/test_workflows.py` asserts the five versions, that
only 3.13/3.14 are experimental, and that the guard exists. **The new jobs have
not run yet** (no CI run from this machine); the PR's UNVERIFIED list says so.

### 2.11 F11 — MED — the main product path is under-tested

**Evidence (measured).** `cli/run.py` **68%** (223 stmts, 60 missed),
`cli/main.py` **75%** (25 missed), `engine/worker.py` **80%** (39 missed). The 90.52%
average hides the weak spots on the code a user actually runs.

**User impact.** Startup, shutdown, signals, reconnect and back-fill — the paths that
fail at 09:15 — have little coverage.

**Fix design (P4).** Target **≥88%** each with meaningful tests: startup with
missing/expired token; HTTP port already in use; SIGINT during back-fill; worker
survives a raising engine (`step()` must not die); queue-full drop accounting;
back-fill auth failure → one alert + clean stop; heartbeat content; watchdog
reconnect; `main()` exit codes per subcommand.

**Tests.** New `tests/cli/test_run_faults.py` (`FakeFeed`, `FakeHistory`, `FakeHub`,
`FakeClock`) and `tests/engine/test_worker_faults.py`.

**Acceptance.** `pytest --cov` shows ≥88% for each module; injected failures produce
the documented user-visible behaviour.

**Risk / rollback.** Tests only.

**Status (P4).** Covered by `tests/cli/test_run_faults.py`, `tests/cli/test_main_faults.py`,
`tests/engine/test_worker_faults.py`. `MarketWorker._deliver()` now wraps
`engine.on_candle` so a raising rule is logged and counted (`engine_errors`) but never
kills the run loop — reproduced on `main` with `RuntimeError` escaping `step()`.
Measured under the full `--cov=kite_alerts` run: `worker.py` 93%, `cli/run.py` 91%,
`cli/main.py` 93% — all above the 88% target (per-submodule `--cov` is unusable on
this machine because it corrupts pandas; see PR UNVERIFIED).

### 2.12 F12 — LOW — four files large enough to hide bugs

**Evidence (`wc -l`).** `ui/settings_schema.py` 1019, `ui/live_feed.py` 862,
`ui/app.py` 818, `ui/chart/service.py` 728; frontend `static/chart.js` 1413,
`settings.js` 639, `practice.js` 549 (≈3.4k lines of JS).

**User impact.** Review cost, merge conflicts; `live_feed.py` mixes transport, state
and SSE.

**Fix design (P5).** Behaviour-preserving split **after** characterization tests:
`live_feed/{transport,state,sse}.py` behind the existing `LiveFeedService` façade;
`app.py` → routers; `chart/service.py` → `candles.py` + `watchlist.py`.

**Acceptance.** Public imports unchanged, coverage not lower, recorded golden payloads
identical before/after.

**Status (P5, this branch).** `ui/live_feed_types.py` now owns the state constants,
`FeedStatus`, `FeedAvailability`, `LiveTick`, `SSEEvent`, `SSEClient`, `TokenRegistry`,
`_backoff_delay`, `_candle_dict` and the badge/availability tables; `ui/live_feed_bootstrap.py`
owns the two history-bootstrap helpers. `live_feed.py` re-exports every name, so all existing
imports and monkeypatches (including `kite_alerts.ui.live_feed._bootstrap_candles`) keep working.
Seven characterization tests were committed **before** the move and pass unchanged. Coverage
rose from 92.74% to 92.76%; `live_feed.py` shrank from 1145 to ~855 lines. The remaining file
splits listed in the fix design are deliberately deferred to keep this branch a pure move.

**Risk / rollback.** Pure refactor; revert the branch if anything shifts.

### 2.13 F13 — LOW — documentation drift

**Evidence.**
- **No `AGENTS.md`** (`ls AGENTS.md` → not found).
- `docs/ROADMAP.md` §2 labels **C1 "Headless Engine & Offline Practice Player"**; C1
  was the dashboard/demo phase, and the engine predates it.
- The same file lists **Docker Compose deployment** as future work although Docker was
  ruled out.
- It claims the live feed has "auto-reconnect with backoff and historical backfill"
  and "throttled pushes (max 4/sec)" as implemented — the backoff constants are dead
  (F24) and backfill is never invoked (F1).
- It claims RSI "with momentum confirmation" while `strategy/rules/rsi_zone.py` is a
  pure zone test.
- `docs/live-feed.md` says "3 websocket connections per **user**"; the official page
  says **3 per API key** (§4).
- The 725-test claim is accurate.

**Fix design (P6).** Write `AGENTS.md` (the six hard rules verbatim + repo commands),
correct ROADMAP's history/future list, mark unimplemented items "planned" and link
this document, add a live-session checklist. Add a docs test forbidding
"arrives in a later update" and unqualified "Docker".

**Risk / rollback.** Documentation only.

### 2.14 F14 — MED — relative paths resolve against the current directory

**Evidence.** `config/settings.py:350` `log_dir: Path = Path("logs")`, `:380`
`db_path: Path = Path("data/kite_alerts.db")`, `:565`, `:568` (same defaults from env).
`cli/run.py:184,195` build the audit and dead-letter files from `settings.log_dir`.
Meanwhile the chart watchlist **is** anchored to the `.env` folder
(`ui/app.py:196`, `env_file.parent / "data" / "chart_watchlist.json"`).

**User impact.** Starting the bot from one folder and the dashboard from another gives
two different databases and two different log sets, with no error. This is the classic
"where did my alerts go?" support ticket.

**Fix design (P2).** Resolve every relative path against the **`.env` file's folder**
(or an explicit `DATA_DIR`); log the resolved absolute paths in both `run` and `ui`;
show them on the About page; warn at startup if the dashboard's DB differs from the one
the bot reports. Expose `/api/about.paths` with the resolved values.

**Tests.** `test_relative_paths_are_anchored_to_env_file` (run both commands with
different `cwd` via `subprocess`/`monkeypatch.chdir`, assert identical resolved paths),
`test_dashboard_warns_when_db_differs_from_bot`, `test_about_page_shows_resolved_paths`.

**Acceptance.** Same `.env` ⇒ same DB and log paths regardless of `cwd`; the warning
fires when they differ; no silent divergence.

**Risk / rollback.** Users with existing data in `./data` keep working because that is
where `.env` lives in the documented quickstart; document the one-time move for anyone
who started elsewhere. Revert restores the old behaviour.

### 2.15 F15 — MED — no frontend tests

**Evidence.** No `tests/js`, no `*.test.js` anywhere, no Playwright in CI, no
`package.json`. `static/*.js` ≈ 3.4k lines (`chart.js` 1413, `settings.js` 639,
`practice.js` 549, `app.js` 349, `connect.js` 304, `dom.js` 105).

**User impact.** SSE reconnect/backoff, token-expiry fallback, badge truthfulness,
stale/market-closed states, form validation and untrusted-text rendering are all
unverified. Badge lying is exactly F2's user-visible symptom.

**Fix design (P4).** Two layers, both free: (a) Node's built-in `node:test` for pure
logic (backoff schedule, badge state→label mapping, price/time formatting, validation
helpers) with the modules refactored to be importable without a DOM; (b) a Playwright
smoke test in CI against `kite-alerts ui --simulate-kite`, asserting per page in light
**and** dark mode: no console errors, **SIMULATED never renders as LIVE**, the
token-expiry control falls back to Delayed with a message, and typed alert text is
rendered as text (no `innerHTML` sink).

**Tests.** `tests/js/*.test.mjs` (node --test) + `tests/e2e_ui/test_smoke.py`
(Playwright marked `e2e`), CI job installing Chromium.

**Acceptance.** Both layers run in CI; the badge-truthfulness assertion fails if the
state machine reports simulated and the badge says LIVE.

**Risk / rollback.** New CI dependency (Chromium download) — cache it; the job can be
non-blocking for one release. Revert = remove the job.

**Audit of the JS (static read).** `chart.js:26,365` hard-codes source availability
(F5); SSE reconnect uses `EventSource`'s implicit retry with no explicit backoff and no
duplicate-connection guard when the symbol changes; `dom.js` centralises text insertion
(no `innerHTML` assignments found in the app modules). The Playwright layer is what
turns these observations into guarantees.

**Status (P4).** Pure logic extracted into `static/badge.js` (state machine +
`staleInfo()`), `static/format.js`, and `static/polling.js`; `dom.js` re-exports the
formatters; `chart.js` consumes `badgeState()`, `latencyText()`, `pollIntervalMs()`.
`tests/js/*.test.mjs` run under Node's built-in test runner (19 passing) via
`npm test`. `tests/e2e_ui/test_smoke.py` drives the real app under Chromium in both
light and dark colour schemes: no console/page errors on any page, DELAYED on load,
SIMULATED (never bare LIVE) on `kite_live`, token expiry falling back to Delayed
with a transient message, and the simulator controls. CI jobs `ui-js` and `ui-browser`
were added; `e2e_ui` tests are excluded from the default pytest run via the marker.

### 2.16 F16 — LOW — delayed/crypto adapters never touched a real provider

**Evidence.** No `scripts/smoke_sources.py`; `nse_delayed` and `crypto_showcase` are
exercised only against fakes. Provider terms/rate limits are not recorded anywhere.

**Fix design (P3).** Add `scripts/smoke_sources.py` (manual, not in CI): fetch one real
sample per provider, print measured lag and result shape, exit non-zero on schema
change. Record provider terms/limits with citations in `docs/data-sources.md`. Keep the
results in §8 as UNVERIFIED until a human runs it with internet access.

**Acceptance.** The script runs against both providers, reports lag, and fails loudly on
a schema change; docs cite each provider's terms page.

**Risk / rollback.** Manual script only.

**Landed (P3).** `scripts/smoke_sources.py` runs the same allowlisted adapters
against the real Yahoo/Kraken endpoints, prints candle count, last-candle UTC
time and measured lag, and exits non-zero on any `SourceError` or shape change.
`tests/unit/test_smoke_sources.py` pins the parsers with captured shapes (the
script itself is missing on `main`, so those tests fail there). `docs/data-sources.md`
gained a provider-terms table (Yahoo ToS, Kraken docs/legal) with an explicit
note that Yahoo does not support this endpoint as an API. **No live run was
possible from this machine**, so the measured lag and current provider shapes
remain in the §8.1 UNVERIFIED list.
---

## 2 (continued). Findings from the full audit

These were **not** in the seed list. Each is verified in the tree.

### 2.17 F17 — MED — the SSE endpoint blocks the event loop

**Evidence.** `ui/app.py:708-760`, `chart_stream`'s async generator runs:

```python
event = client.get(timeout=0.5)      # blocking queue.get inside an async generator
...
await asyncio.sleep(0.01)
```

`SSEClient.get` (`live_feed.py:286-...`) blocks the calling thread. Because this runs in
the **event loop**, one idle browser tab stalls every other request (settings save,
chart fetch, test alert) for up to 0.5 s per iteration, and N tabs multiply it. The same
function also reads `len(context.feed._sse_clients)` — a private list — **without**
`_sse_lock`, so the 50-client cap can be exceeded by concurrent connects.

**User impact.** The dashboard becomes unresponsive whenever the live chart tab is open;
under load the stream cap is not actually enforced.

**Fix design (P0, small).** Push events from a per-client thread-safe queue into the
generator with `await asyncio.to_thread(client.get, 0.5)` (or `anyio.to_thread.run_sync`),
send the heartbeat from a monotonic timer, and expose
`feed.register_sse_client(client) -> bool` that enforces the cap under `_sse_lock` and
returns 429 via a normal HTTPException.

**Tests.** `test_sse_does_not_block_the_event_loop` (a concurrent `GET /api/health` must
complete while a stream is idle; fails on `main`), `test_sse_cap_is_enforced_under_
concurrency` (barrier + 60 threads → exactly 50 accepted, 10 rejected with 429).

**Acceptance.** Health latency stays <100 ms with 5 idle streams; 51st stream gets 429.

**Risk / rollback.** Touches the streaming path only; the Playwright smoke test (F15)
covers it end to end.

### 2.18 F18 — MED — the notifier queue is unbounded

**Evidence.** `notifiers/hub.py:61` → `self._queue: queue.Queue[object] = queue.Queue()`
(no `maxsize`), while `engine/worker.py` correctly bounds its tick queue with
`settings.resilience.tick_queue_size`. `docs/ROADMAP.md` §1 claims bounded queues and a
drop policy; the worker has one, the notifier does not.

**User impact.** If Telegram/Slack stalls for hours, alerts accumulate in memory for the
whole session; a long-running bot can be OOM-killed, and the "final delivery" ordering
silently becomes a backlog that fires stale alerts when the channel recovers.

**Fix design.** Bound the hub queue (`NOTIFY_QUEUE_MAX`, default 5 000), define overflow
explicitly: **drop-oldest for `system` messages, keep the newest alerts, and always count
drops** (`notifier_dropped_total`) with a warning log. Document the policy next to the
tick-queue policy.

**Tests.** `test_notifier_queue_is_bounded`, `test_overflow_drops_oldest_system_message`,
`test_overflow_keeps_newest_alerts_and_counts_drops`, `test_backlog_does_not_grow_unbounded`
(10k sends against a blocked channel, assert queue size ≤ max and counter == expected).

**Acceptance.** Memory is bounded under a blocked channel; metrics reflect drops; no alert
is lost silently without a counter.

**Risk / rollback.** Dropping messages is a behaviour change; keep the default generous
(5 000) and revert if any delivery-ordering test regresses.

**Landed (P2) and close-out.** `NOTIFY_QUEUE_MAX` bounds the hub; the shipped policy
is: **an incoming `system`/`info` message is dropped when the queue is full** (status
text never displaces an alert); an **incoming alert evicts the oldest `system` message,
else the oldest alert**; every drop increments `notifier_dropped_total` and logs; every
evicted or dropped **alert** is also appended to the dead-letter log
(`channel="queue-overflow"`) so it stays recoverable.

The first pass claimed that policy but did not implement all of it. Closed on
`fix/p2-closeout`, each with a test that failed on `bf7fb25`:

* `test_full_queue_drops_incoming_system_message` — `['alert', 'system']` was
  delivered: a status line evicted a real alert (violates the documented policy);
* `test_overflow_records_evicted_alert_in_dead_letter_log` — `dlq.count == 0`: a
  dropped alert vanished with only a counter;
* `test_backlog_does_not_grow_unbounded` — the backlog test from the plan was
  missing; it now drives a 2 000-alert burst against a blocked channel and asserts
  depth ≤ max, an exact drop count, `metrics == drops`, and `dlq.count == drops`.
  The plan said 10k; 2 000 keeps ~2 000 dead-letter writes out of CI's critical
  path while still exercising the bound for 40 full-queue rounds.

Two implementation details worth recording. The eviction decision and the enqueue
happen under the queue's own mutex: `put_nowait` raising `Full` and *then* evicting
lost an extra alert whenever the consumer freed a slot in between (the standalone
repro showed `dropped=9948` with `dlq=9950` for a 10k burst). Dead-letter bookkeeping
runs outside the mutex so a producer's disk write never stalls the worker. `_STOP` is
still always enqueued, so `close()` cannot deadlock on a full queue.

### 2.19 F19 — LOW — the "forming" candle is computed from the wall clock

**Evidence.** `live_feed.py:_broadcast_forming` uses
`pd.Timestamp.now(tz=IST) + pd.Timedelta(minutes=1)` and
`self._builder.pending(...)`, and binds an unused local (`in_progress = ...`). Tick time
(`tick.exchange_ts`) is what the builder uses; the broadcast therefore derives the
"forming" candle from receipt time.

**User impact.** Near a minute boundary the chart can show the previous minute as
"forming" while the tick belongs to the new one, and the forming candle visibly jumps.

**Fix design.** Pass the tick's own timestamp into `_broadcast_forming(symbol, tick_ts)` and
read `builder.in_progress()` for the current minute instead of `pending()`.

**Tests.** `test_forming_candle_uses_tick_time_not_wall_clock` (frozen clock, tick stamped
09:16:59 → forming minute 09:16), `test_no_forming_event_for_a_stale_minute`.

**Acceptance.** Forming-candle minute always equals the floor of the tick's IST timestamp.

**Risk / rollback.** Display-only; revert is trivial.

### 2.20 F20 — LOW — stop() permanently poisons the feed

**Evidence.** `live_feed.py:388-410`: `start()` sets `_stopped` semantics such that a later
`start()` returns early with `log.warning("cannot restart after stop(); create a new
instance")`; `stop()` sets `_stopped = True` permanently. Nothing in the dashboard ever
recreates the instance (`ui/app.py:190-213` constructs it once). Yet
`docs/live-feed.md` §2 says the feed "can be paused and resumed" and F1's fix depends on
being able to start after a connect.

**User impact.** Any future "Disconnect then Connect" (or a token refresh that stops the
feed) leaves a dead service with no recovery except restarting the dashboard.

**Fix design.** Split `stop()` (shutdown, terminal) from `pause()` (close ticker, keep the
service usable) and have `start()` create a fresh `KiteTicker` when `_ticker` is `None`.
The reactor cannot restart, so a *credential-driven* rebuild must reuse the running
reactor: create a new `KiteTicker` on the same loop (Kite's client allows a new instance
per connection) and keep exactly one live one (§3e).

**Tests.** `test_start_after_pause_creates_a_new_ticker`, `test_start_after_shutdown_is_
refused_with_a_message`, `test_token_rotation_rebuilds_without_restarting_the_process`.

**Acceptance.** Pause/resume works; terminal shutdown still refuses politely; no second
reactor.

**Risk / rollback.** This is the riskiest concurrency change in P0; land it behind the
state machine tests from §3c and keep the old behaviour behind a flag for one release.

### 2.21 F21 — LOW — watchlist writes race

**Evidence.** `chart/service.py:575-580` writes `chart_watchlist.json.tmp` then
`os.replace`, with **no lock**, and `add_watchlist`/`remove_watchlist` do
read-modify-write on the whole file. Two concurrent requests share the same temp path.

**User impact.** Double-click "add" can lose one symbol or (rarely) leave a corrupt file.

**Fix design.** A single `threading.Lock` in `ChartService` around read-modify-write, and a
temp name that includes `os.getpid()`+`uuid4` to avoid collisions.

**Tests.** `test_concurrent_adds_keep_every_symbol` (barrier + all 10 slots),
`test_temp_file_name_is_unique_per_writer`.

**Acceptance.** Every concurrent add within `WATCHLIST_LIMIT` survives, file parses, no
stray temp files.

**Risk / rollback.** Local; revert.

**Landed (P2b).** `ChartService` now has `_watchlist_lock` around the whole
read-modify-write, and `_write_extras` uses `chart_watchlist.json.<pid>.<uuid4>.tmp`,
removing it if the write fails. A first fix that locked only the writers still failed
the full suite: a **reader** (`watchlist()` → `_read_extras`) could hold the file open
while another thread's `os.replace` ran, which Windows rejects with
`PermissionError(13, "Access is denied")`. The lock is therefore a `RLock` taken by both
`_read_extras` and the add/remove read-modify-write (the RLock because add/remove already
hold it when they call `_read_extras`). Two tests fail on `bf7fb25`:

* `test_temp_file_name_is_unique_per_writer` — both writers used
  `chart_watchlist.json.tmp`;
* `test_concurrent_adds_keep_every_symbol` — on Windows the shared temp name made
  concurrent adds fail with `PermissionError(13, "The process cannot access the file
  because it is being used by another process")`, i.e. the dashboard returned 500s.

The plan's "20 threads / 20 symbols" was wrong: `WATCHLIST_LIMIT` caps the user list at
10, so the test drives the 10 legal slots and asserts the file contains all of them.

### 2.22 F22 — LOW — unsynchronised shared dicts in the feed

**Evidence.** `_last_push` (`live_feed.py`, written in `_handle_tick` from the ticker
thread and read on the same path), `_bootstrap_data` (written by bootstrap threads,
read by request threads via `bootstrap_data_for`), and `_registry` (written inside
`subscribe()` under `_lock`, read outside it in `_process_raw_tick`) — **the
`_registry` claim was wrong**: it has had its own internal lock since it was written
(`TokenRegistry.register/symbol_for`), so the audit corrected this finding rather than
"fixing" working code.

**User impact.** Currently benign under CPython (atomic dict ops), but it is unsynchronised
state that a refactor (or a free-threaded build) can break, and the code claims a
lock discipline it does not follow. One real defect was reproducible: `bootstrap_data_for`
returned the live list, so one request could append to the cache every later request read.

**Fix design.** Guard `_registry` reads with `_lock` (or make `TokenRegistry` internally
locked), hold `_lock` for `_bootstrap_data` writes, move `_last_push` into the locked
section that already exists, and document the one lock order: `_lock → _sse_lock →
builder._lock`.

**Tests.** `test_registry_reads_are_locked` is not observable from outside; instead add a
ThreadSanitizer-style stress test (`slow` mark): 4 writers + 4 readers for 2 s asserting no
exceptions and consistent counts.

**Acceptance.** The stress test passes repeatedly; the documented lock order has no
inversions (grep-audited).

**Risk / rollback.** Purely defensive.

**Landed (P2b).** `_last_push` is now read-modified-written inside the tick lock
(`_handle_tick`), `_bootstrap_data` writes happen under `_lock`, and
`bootstrap_data_for` returns a shallow copy (the deterministic failing test on
`bf7fb25`: a caller's `append` changed what every later read returned). The lock order
is documented on the dataclass fields. The stress test (4 ticker + 2 reader threads,
200/400 iterations) passes on `bf7fb25` too — as the finding says, dict ops are atomic
under the GIL, so F22 is hardening, not a live-fire bug; the copy is the one part with
user-visible behaviour.

### 2.23 F23 — LOW — the golden file's line endings depend on the checkout

**Evidence.** `git show HEAD:demo_output.txt` is LF; the working tree file is CRLF because
the bundled Git sets `core.autocrlf=true` and the repo has **no `.gitattributes`**. The demo
writes LF (`ensure_utf8_stream(newline="\n")`), so a raw `diff` fails on Windows while
`scripts/check_demo_golden.py` passes (it compares text).

**User impact.** A contributor on Windows sees a failing "golden" diff and may "fix" it by
changing the generator. CI on Linux is unaffected.

**Fix design.** Add `.gitattributes` with `* text=auto eol=lf` and `*.png binary`, and make
`check_demo_golden.py` the only sanctioned comparison (it already normalises).

**Tests.** A repo-hygiene test asserting `.gitattributes` exists and pins `eol=lf` for
`demo_output.txt`.

**Acceptance.** Fresh Windows checkout: `git status` clean, golden script green, raw diff of
a regenerated file green too.

**Risk / rollback.** Re-normalising line endings touches many files; do it in its own commit
with `git add --renormalize .` and verify the tree is unchanged apart from endings.

**Landed (P3).** `.gitattributes` pins `* text=auto eol=lf` and marks common
image types binary. The index already stored LF, so `git add --renormalize .`
produced no content diff. `tests/unit/test_repo_hygiene.py` asserts the rules
exist, that `git check-attr` reports `eol: lf` for `demo_output.txt`, and that no
tracked `.py`/`.md`/`.yml`/`.yaml` file is stored with CRLF in the index.

### 2.24 F24 — LOW — documented reconnect backoff does not exist

**Evidence.** `live_feed.py:61-62` defines `_BACKOFF_BASE = 1.0`, `_BACKOFF_CAP = 60.0`;
neither is referenced anywhere (grep). `docs/live-feed.md` §4 promises
"exponential backoff (1 s → 60 s with jitter)". Reconnection is whatever `KiteTicker`'s own
`reconnect=` defaults do (the dashboard does not even pass `reconnect_max_tries`/
`reconnect_max_delay`, unlike `data/feed.py:_build_ticker`). `_CLOCK_SKEW_WARN` is also
unused.

**User impact.** During a network blip the dashboard's reconnect cadence is unknown and
unbounded; the docs describe behaviour the code does not implement.

**Fix design (P0).** Either implement the backoff (own timer + jitter, cancel on
`on_connect`, cap at 60 s) or delete the constants and correct the docs, passing
`reconnect=True, reconnect_max_tries=..., reconnect_max_delay=...` like the bot's feed.
Prefer **implement**: the UI needs a deterministic retry to show "Retrying in Ns".

**Tests.** `test_backoff_schedule_is_1_2_4_..._capped_60_with_jitter` (injected RNG),
`test_reconnect_counter_and_state_sequence` (fake ticker dropping twice),
`test_backoff_is_cancelled_on_connect`.

**Acceptance.** Reconnect intervals follow the documented schedule; the UI shows the next
retry; the reconnect counter matches reality.

**Risk / rollback.** Touches connection lifecycle; keep `KiteTicker`'s own reconnect off
when ours is active to avoid double retries.

### 2.25 F25 — LOW — cached calendar keyed by object identity

**Evidence.** `chart/service.py:_calendar` caches `(id(settings), calendar)`. `id()` is only
unique among live objects; after a settings reload and GC, a new `Settings` can reuse the
address and receive a stale calendar (wrong open/close/holiday set).

**User impact.** Rare, but a settings change could silently keep the old session window until
a restart — the sort of bug that is invisible until a holiday or a changed close time.

**Fix design.** Key the cache by the values that matter (`open_time`, `close_time`,
`holiday_file`, plus a content hash/mtime) instead of `id()`.

**Tests.** `test_calendar_cache_invalidates_when_session_window_changes` and
`test_calendar_cache_survives_equal_settings`.

**Acceptance.** Changing session times takes effect on the next request; no stale calendar.

**Risk / rollback.** Small, local.

**Landed (P3).** The cache key is now
`(open_time, close_time, pre_open_time, closing_end, stamp(holiday_file),
stamp(special_sessions_file))`, where a stamp is `(resolved path, mtime_ns, size)`
(or a distinct missing-file sentinel). Equal settings from a reload share one
calendar, a changed session window rebuilds it, and editing `holidays.yaml` is
picked up on the next request without a restart. Tests:
`test_equal_settings_share_one_calendar`,
`test_changed_close_time_rebuilds_the_calendar`,
`test_editing_the_holiday_file_invalidates_the_cache` (the first and the last
fail on the base: identity keying gives a reload a different calendar, and the
same object keeps a stale one after the YAML changes).

### 2.26 F26 — LOW — `.env` read-modify-write is unsynchronised

**Evidence.** `ui/settings_store.py:236-266` reads the file, applies updates, writes a temp
and replaces; `cli/login_cmd.py:47-50` does the same for `KITE_ACCESS_TOKEN`. There is no
shared lock, and the dashboard can save settings from a FastAPI worker thread while the
OAuth callback writes the token.

**User impact.** A login landing between another save's read and replace silently drops the
other field (classic lost update).

**Fix design.** One small `EnvFile` helper (single `threading.Lock` per resolved path, plus
an `fcntl`/`msvcrt` advisory lock for cross-process writes) used by both call sites; always
re-read inside the lock.

**Tests.** `test_concurrent_save_and_token_write_loses_nothing` (barrier + two threads saving
disjoint keys 50 times), `test_cross_process_lock_is_released_on_crash` (subprocess killed
mid-write; second writer proceeds).

**Acceptance.** No lost keys under concurrent writes; both consumers share the helper.

**Risk / rollback.** Local to two modules; revert restores today's behaviour.

### 2.27 F27 — LOW — a late `on_connect` can resurrect a stopped feed

**Evidence.** `ui/live_feed.py::_on_connect` checked `_generation_matches(ws)` *outside*
`self._lock`, then took the lock to set `STATE_LIVE`. `stop()` sets `_stopped` and bumps
`_generation` in two separate critical sections, so a callback landing between the
check and the state write could claim LIVE for a service that is already shutting
down. `_flush_subscriptions()` likewise only checked for a missing ticker.

**User impact.** A `stop()` during a reconnect handshake can leave the UI showing a
live/connected feed after shutdown, and can subscribe tokens on a socket that is
being torn down.

**Fix design (P4).** Move the generation check *inside* the same `with self._lock`
block as the state write, and also reject when `_stopped` is set. Make
`_flush_subscriptions()` return early when `_stopped`.

**Tests.** `tests/unit/test_live_feed.py::TestStopIsTerminal` pauses `stop()` between
the `_stopped` write and the socket close, fires `_on_connect` in that window, and
asserts no status broadcast, no subscription, and a terminal `disconnected` state.

**Acceptance.** The regression test fails on `main` (late callback broadcasts
`['live']`) and passes with the fix.

**Risk / rollback.** Touches only the connect-callback path; revert restores the
race.

---

## 2.N Findings from the connected-user walkthrough (P0.1)

These were found *after* the Stage 1 audit, by walking the code paths a real
connected user takes (login → start → live → mid-session token expiry) with a
strict fake socket. Reproduced on `main` = `fe30686` with a throwaway script;
the script's output is quoted here and in the PR.

### N1 — HIGH — after a mid-session token expiry, every surface kept saying LIVE

Repro output on `main` (strict fake ticker; a fresh tick was pushed, then a
403 `TokenException` was raised mid-session):

```
STEP connecting data-sources: LIVE | True | Connecting to the Kite live ticker...
STEP live candles status: 200
STEP live payload: badge= LIVE | notice= Streaming live ticks from Kite. | feed_state key present? False
STEP expiry feed state: token_expired
N1 AFTER-EXPIRY candles status: 200
N1 AFTER-EXPIRY payload: badge= LIVE | notice= Connecting to the Kite live ticker... | state= <missing>
tickers created: 2
```

Three lies in one response: the candles endpoint answered **200 with badge
`LIVE`** for a feed whose token had just died, the notice was the stale
"Connecting to the Kite live ticker..." string, the payload carried **no feed
state at all**, and the chart poll **built a second ticker with the same dead
token** (`chart_candles` → `FeedController.ensure_started()` → `start()`,
which happily restarted). The `KiteLiveSource.badge` property was hard-coded
"LIVE" except for the simulator, and `updateLiveBadge` in `chart.js` had no
branch for `stale`, `no_plan` or `disconnected`.

Fixed on this branch:

* `live_feed.py`: `_BADGE_BY_STATE` — one badge per feed state; "LIVE" appears
  only for fresh-tick `live`. `start()` refuses a restart with unchanged
  credentials while in `token_expired`/`no_plan`; `has_credentials()` exposes
  the check; `set_token_expired()` is idempotent; `_on_noreconnect` keeps the
  `token_expired` state instead of regressing to `disconnected`.
* `feed_controller.py`: `ensure_started()` early-returns `False` for a terminal
  state whose credentials are unchanged (so a chart poll can no longer restart
  a dead feed; a *fresh login* still restarts it — tested).
* `chart/service.py`: the candles and symbols payloads carry `feed_state`.
* `chart/sources.py`: `KiteLiveSource` reads badge/notice/enabled from the one
  availability view (legacy doubles without `availability()` fall back to a
  state→badge map, so the pinned `STATE_LIVE → "LIVE"` behaviour is kept).
* `chart.js`: `updateLiveBadge` branches for connecting/stale/market_closed/
  token_expired/no_plan/disconnected; only the fresh-tick branch renders "LIVE".
* `styles.css`: visible styles for CONNECTING/STALE/CLOSED/EXPIRED/NO PLAN/OFF.

### N2 — MED — `/api/data-sources` lagged reality by up to 60 s

`data_sources()` cached the **whole payload** (token, timestamp, sources) for
`DATA_TTL_SECONDS` = 60. Same repro, no `?refresh=1`:

```
STEP live data-sources: LIVE | True | Connecting to the Kite live ticker...   <- while streaming
N2 AFTER-EXPIRY data-sources: LIVE | True | Connecting to the Kite live ticker...  <- 1 min after expiry
```

The picker advertised "Connecting..." while ticks were flowing and "LIVE" for
a minute after the token died. Fixed: only the **plan probe** is cached now
(token + TTL, `?refresh=1` still bypasses it — the probe is the only network
call); every badge/notice/enabled/`state` is recomputed from
`feed.availability()` on each call. `DataSource` gained a `state` field so the
picker can show exactly which feed state it rendered.

### N3 — MED — the SSE stream loop had no tests at all

On `main`, `ui/app.py` lines 762-787 (the `chart_stream` loop: the
`asyncio.to_thread` drain and `admit_sse_client` cap, i.e. the whole F17 fix)
had **0% coverage**. `tests/unit/test_sse_stream_lifecycle.py` now drives the
raw ASGI interface with a strict fake socket: responsiveness while one client
is slow (and `SSEClient.get` never runs on the event-loop thread), the 50-client
cap enforced under a barrier race, clean disconnect/unregister, and no leaked
threads across sequential streams. **These pass on `main`** — they guard the
existing F17 fix and are documented as such in the module docstring; the
P0.1-specific failures were the badge-truth tests.

### N4 — LOW — the holiday list is empty on this machine, by design

`scripts/refresh_holidays.py` exists and its HTML parser is fixture-tested
(`tests/unit/test_calendar_holidays.py`, 12 tests, green). The live fetch
cannot be verified here: `https://www.nseindia.com/...` completes the TLS
handshake and then the **read times out** (NSE drops non-browser clients).
Per the audit rule (never invent holiday dates), `config/holidays.yaml` stays
empty, the startup warning stays, and running the script once manually against
the official page is recorded as an owner step (also listed in §8.1).

### N5 — INFO — phase assignments for the leftover findings

Per owner instruction: **F18 → P2**, **F21, F22 → P2b** (a new phase for the
concurrency clean-ups), **F23, F25 → P3**. The snapshot table in §0 records
this so the phased plan matches what will actually be built next.

---

## 3. Concurrency design

### 3.a Thread inventory

| # | Thread / task | Created by | Owns / does | Started | Stopped | Exit path |
|---|---|---|---|---|---|---|
| 1 | **uvicorn event loop** (main) | `ui/server.py:run` | HTTP + SSE coroutines, `lifespan` | process start | Ctrl-C / SIGTERM | uvicorn shutdown → `lifespan` finally: `simulator.stop()`, `feed.stop()` (`app.py:350-357`) |
| 2 | `kite-ticker-connect` | `LiveFeedService._start_ticker` (`live_feed.py:600`) | calls `ticker.connect(threaded=True)` then (today) subscribes | `feed.start()` | ends when `connect` returns | thread exits immediately; `self._thread` kept but never joined |
| 3 | **Twisted reactor** (inside `kiteconnect`) | `KiteTicker.connect(threaded=True)` | websocket I/O, decodes frames, invokes `on_ticks`/`on_connect`/`on_close`/`on_error`/`on_reconnect`/`on_noreconnect` | first connect | `ticker.close()` (on `feed.stop()`) | reactor thread ends when the process ends; **cannot be restarted** |
| 4 | `kite-bootstrap-<symbol>` | `LiveFeedService.run_bootstrap` | historical fetch → `_bootstrap_data` | (never, today) | self-terminating | daemon; lost on exit |
| 5 | `kite-simulator` | `KiteSimulator.start` (`simulator.py:110`) | generates ticks, calls `feed.push_simulated_tick` | `--simulate-kite` | `simulator.stop()` | stop flag checked each loop (`:168-173`), daemon |
| 6 | **SSE handler coroutine** × N | FastAPI, per `GET /api/chart/stream` | drains `SSEClient` queue, heartbeats | request | client disconnect | `finally`: `unregister_sse_client`, `close` |
| 7 | `kite-alerts-worker` (bot process) | `MarketWorker.start` (`worker.py:143`) | **sole owner** of builder, engine, watchdog, back-fill | `kite-alerts run` | `stop_event` + join(10s) | loop exits on `stop_event` (`:150-156`) |
| 8 | `kite-alerts-notifier` (bot) | `NotifierHub.start` (`hub.py:92`) | delivery, retries, rate limits, DLQ | `kite-alerts run` | `join(timeout)` (`:100`) | sentinel on the queue |
| 9 | `kite-alerts-http` (bot) | `observability/health.py:95` | `/health`, `/metrics` (`http.server`) | settings | `shutdown()` | `serve_forever` returns |
| 10 | KiteTicker reconnect timers (bot) | `kiteconnect`, when `reconnect=True` | scheduled retries | connect | socket close | inside the reactor thread |

Two invariants worth stating because nothing enforces them today:

- **Thread 3 and thread 1 must never share mutable state without a lock.** Today the
  SSE list is the only bridge, guarded by `_sse_lock` — except for the unlocked
  `len(...)` read in `app.py` (F17).
- **The bot (threads 7–9) and the dashboard (1–6) are separate processes.** They share
  only SQLite (WAL) and `.env`. Nothing else may cross.

### 3.b Shared-state map

| Object | Single owner | Guard | Invariants | Rule violations today |
|---|---|---|---|---|
| `LiveFeedService._state/_reason/_reconnect_count/_last_tick_*` | reactor callbacks + request threads | `self._lock` | every UI status derives from `_state`; `LIVE` ⇒ a tick arrived while connected | `status()` re-derives STALE/MARKET_CLOSED outside the lock (ok, read-only) |
| `_subscribed_tokens`, `_futures_tokens` | `subscribe()`/`unsubscribe()` | `self._lock` | mirror of what the exchange accepted | **not true** until F2 is fixed (records intent) |
| `_registry` (`TokenRegistry`) | `subscribe()` | internal lock; `subscribe()` also holds `_lock` | token→symbol stable | none — corrected in P2b: it was always internally locked |
| `_sse_clients` | request threads + reactor | `_sse_lock` | ≤50 clients | unlocked `len()` in `chart_stream` (F17) |
| `_last_push` | reactor thread | `self._lock` (since P2b) | per-symbol throttle ≤4/s | none |
| `_bootstrap_data` | bootstrap threads | `self._lock` (since P2b); reads return a copy | snapshot per symbol never mutated by readers | none |
| `CandleBuilder` (`_open/_last_seen/_emitted`) | reactor thread (feed) / worker thread (bot) | internal `RLock` | one open candle per symbol; emitted minutes never re-emitted | I/O under lock: none observed |
| `Engine` (history, cooldown, params) | worker thread (bot) / demo thread (UI) | own lock for `stats()` | frozen `StrategyParams` | none found |
| `MarketWorker._queue` | websocket thread → worker thread | `queue.Queue(maxsize)` | bounded, drop-new on full, drops counted | correct |
| `NotifierHub._queue` | producers → notifier thread | `queue.Queue(maxsize=NOTIFY_QUEUE_MAX)` | bounded: incoming system dropped; alert evicts oldest system, else oldest alert; drops counted + DLQ (F18) | correct |
| `AlertStore` (SQLite) | bot writer; dashboard reader | `RLock` + one connection, WAL | dedup ledger is the source of truth | dashboard uses its own read-only connection (WAL ⇒ readers never block the writer) |
| `.env` | `SettingsStore.save()` and `cli.login_cmd.write_access_token()` | `EnvFile` lock (thread + OS advisory) | never partially written | correct |
| `chart_watchlist.json` | `ChartService` | `_watchlist_lock` + per-writer temp (since P2b) | read-modify-write whole file | correct |

**Lock discipline to adopt (and document in `AGENTS.md` when P6 lands):**

1. **Order:** `LiveFeedService._lock` → `LiveFeedService._sse_lock` → `CandleBuilder._lock`.
   Never the reverse. Never hold two of them for longer than a dict operation.
2. **No I/O and no callback into user code while holding a lock.** Today `subscribe()`
   calls `ticker.subscribe()` after releasing the lock — keep it that way; `stop()`
   calls `ticker.close()` after release — keep that too.
3. **Every queue bounded with a declared overflow policy**: worker = drop-newest
   (counted), notifier = drop-oldest-system / keep-newest-alert (counted, F18), SSE =
   drop-oldest (already, `_MAX_SSE_QUEUE=500`).
4. **No nested locks across threads that both take a lock in different orders**; the
   dashboard's only cross-thread handoff is a queue.

### 3.c State machine

`LiveFeedService` (the only source of any status the UI shows):

```
                 start()                       on_connect
  DISCONNECTED ───────────► CONNECTING ────────────────────► LIVE
       ▲                        │                              │
       │ on_close / on_error    │ connect/create failure       │ no tick > 30 s
       │ (non-auth)             ▼                              ▼
       └──────────────────── DISCONNECTED                 STALE ──tick──► LIVE
       │                                                    │
       │  _market_is_open() == False                        │ market closed
       ▼                                                    ▼
  MARKET_CLOSED ◄───────────────────────────────────────────┘
       │
   on_error 401/403 or app_code 1004 → TOKEN_EXPIRED (terminal until reconnect)
   --simulate-kite                   → SIMULATED (permanent, never LIVE)
   plan probe fails                  → NO_PLAN (currently unreachable — F5)
```

| State | Entered by | Allowed next | Side effects |
|---|---|---|---|
| `disconnected` | init, `stop()`, non-auth error, close | `connecting` | badge grey; candles endpoint must say "not connected" |
| `connecting` | `start()` | `live`, `disconnected` | badge amber "Connecting…"; flush subscriptions on entry to `live` |
| `live` | `on_connect` | `stale`, `market_closed`, `token_expired`, `disconnected` | badge green **only here**; ticks flow |
| `stale` | `status()` when age > 30 s and market open | `live` (tick), `market_closed` | badge amber "Stale since HH:MM:SS" |
| `market_closed` | `status()` when stale and market closed | `live` | badge amber "Market closed" |
| `token_expired` | `on_error` 401/403/1004, plan probe auth failure | `connecting` (after reconnect) | red badge + "Reconnect"; chart falls back to Delayed |
| `no_plan` | plan probe 403/permission | `connecting` | badge amber + upgrade note |
| `simulated` | `--simulate-kite` | — (never) | purple badge; **never** `live` |

Rules: exactly one state at a time; `simulated` overrides every other except
`token_expired`/`disconnected` (so a simulated feed can show its failure); **every**
UI string must come from this table (no component may invent a badge); transitions happen
only inside `LiveFeedService` methods, never in route handlers.

### 3.d Race scenarios and resolutions

| # | Scenario | Today | Chosen resolution | Test |
|---|---|---|---|---|
| 1 | `start()` racing `stop()` | `_lock` set then thread start outside; a concurrent `stop()` can leave a live ticker with `_stopped=True` | Do the whole transition under `_lock`, publish the state, *then* start the thread; `stop()` cancels a pending start via a generation counter | `test_start_stop_race_ends_disconnected` (barrier) |
| 2 | Double start from rapid picker clicks | early-return on `CONNECTING/LIVE` (ok) but two `connect` threads can still be spawned before state is set | set `CONNECTING` under lock **before** spawning; second call returns | `test_rapid_double_start_creates_one_ticker` |
| 3 | Subscribe before connect (**F2**) | raises, closes socket, badge says LIVE | queue + flush in `on_connect` | `test_subscribe_is_never_called_before_on_connect` |
| 4 | Subscribe during reconnect | `_on_reconnect` sets `CONNECTING`; queued subs are only flushed by `_start_ticker` (never again) | flush in both `on_connect` and `on_reconnect` | `test_subscriptions_survive_reconnect` |
| 5 | Token rotation while ticks arrive | no path at all (F1) | `rebuild(api_key, token)`: pause → new ticker on the same reactor → flush queue → resume; ticks from the old ticker are dropped by generation | `test_token_rotation_discards_old_generation_ticks` |
| 6 | Reconnect backfill vs live ticks (dup/out-of-order/overlap) | `CandleBuilder` drops late + duplicate, remembers emitted minutes (600) | keep; backfill must run **before** flushing subscriptions, and `builder.reset()` on gap (as the bot does) | `test_backfill_overlapping_live_candle_keeps_one` |
| 7 | Builder fed by ticks **and** the minute timer | `close_completed` on the worker; feed builder only gets ticks (no timer) → an illiquid symbol's last candle never closes in the chart | feed builder needs the same minute ticker (a 1 s timer calling `close_completed`) | `test_feed_builder_closes_on_clock_without_ticks` |
| 8 | SSE client disconnects during broadcast | `push` to a closed client (suppressed) | keep, but unregister in `finally` (already) and never hold `_sse_lock` across a socket write | `test_broadcast_with_client_dropping_mid_stream` |
| 9 | Slow SSE client overflow | drop-oldest at 500 (ok) | keep; assert the counter | `test_slow_client_is_trimmed_not_unbounded` |
| 10 | Shutdown with in-flight bootstrap | bootstrap threads are daemons; `_bootstrap_data` may be half-written | join bootstraps with a 2 s deadline in `feed.stop()` | `test_shutdown_joins_bootstrap_threads` |
| 11 | Settings reload while the feed runs | `reload()` swaps `Settings` and history; the feed keeps old credentials | compare `(api_key, token)`; rebuild only if changed | `test_settings_reload_without_token_change_does_not_reconnect` |
| 12 | Watchlist file written by two requests | shared temp, no lock (F21) | lock + unique temp | `test_concurrent_adds_keep_every_symbol` |
| 13 | `.env` written by UI and by `kite-alerts login` | lost update (F26) | one `EnvFile` helper with a lock | `test_concurrent_save_and_token_write_loses_nothing` |
| 14 | SQLite dashboard reader vs bot writer | WAL + own connection; bot uses `timeout` default 5 s | set `busy_timeout=5000` explicitly and `PRAGMA query_only=1` on the reader | `test_reader_never_blocks_writer_and_sees_committed_rows` |
| 15 | Clock jumps / PC skew | latency guard `<0 or >300 s` → `None` | keep; add a UI note when `abs(skew) > 30 s` (`_CLOCK_SKEW_WARN`, currently unused) | `test_negative_latency_is_reported_as_clock_skew` |
| 16 | SSE request during shutdown | new stream can register after `stop()` cleared the list | `register_sse_client` returns False when stopped → 503 | `test_stream_rejected_after_shutdown` |

### 3.e The Twisted reactor limitation

`KiteTicker` runs Twisted's reactor; `reactor.stop()` is **terminal for the process**
(`ReactorNotRestartable`). `docs/live-feed.md` §2 already describes the intended
long-lived-singleton design, and that is the design I keep:

- **One reactor, one ticker at a time.** `connect(threaded=True)` on first start; the
  reactor thread lives for the whole dashboard process.
- **Pause ≠ shutdown.** `pause()` closes the ticker's socket without stopping the
  reactor; `start()` after a pause creates a **new** `KiteTicker` instance and connects it
  on the already-running loop. This is what makes "Disconnect → Connect" and token
  rotation work without restarting the process (F20).
- **Credential change:** `rebuild(api_key, token)` — pause, bump a generation counter
  (so late frames from the old ticker are ignored), create + connect the new ticker, flush
  the pending subscriptions on its `on_connect`, restart the backoff timer.
- **Terminal `stop()`** stays terminal and says so in plain language ("Restart the
  dashboard to use live data again").
- **Why not a supervised subprocess now?** A child process would give a clean restart but
  costs: a second SSE bridge, IPC for ticks, another failure surface, and Windows
  packaging complexity. It is deferred; the interface (`ensure_started/pause/rebuild/
  status`) is designed so a subprocess implementation can slot in behind it in P5 if
  pause/rebuild proves fragile in the live session.
- **Testing:** all of the above is testable with `FakeTicker` (no Twisted at all):
  `test_pause_start_creates_second_ticker_same_reactor`,
  `test_rebuild_ignores_old_generation_frames`, `test_terminal_stop_refuses_restart`.
  The one thing that cannot be tested offline is whether Kite's client tolerates a second
  `KiteTicker` on a live reactor — that is item 1 of the manual checklist (§8).
---

## 4. Domain rules to encode

Each rule is marked **[verified]**, **[to verify]** (memory / needs the official page) or
**[cited]**. Citations are to the official Kite Connect v3 docs and nseindia.com; where a
detail is not on the page I actually read, it is marked *to verify* rather than asserted.

### 4.1 WebSocket modes, limits, and what each mode gives

- **Modes are `ltp`, `quote`, `full`**; `set_mode(mode, tokens)` switches per token.
  `full` adds the depth/order-book fields and open interest; `quote` adds volume, OHLC and
  best bid/ask. **[to verify]** — the exact field list per mode should be copied from the
  WebSocket page's table into `docs/live-feed.md` before P0 ships.
- **3 000 instruments per connection; 3 connections per API key.** **[cited]**
  <https://kite.trade/docs/connect/v3/websocket>. Our cap is `MAX_SUBSCRIPTIONS = 25`
  (`live_feed.py:56`), which is safe but must be *documented as our choice*, not as
  Kite's limit (the docs currently conflate the two — F13).
- **OI is only meaningful for F&O instruments and only in `full` mode.** **[to verify]**
  against the mode table; the code already switches futures to `full`
  (`live_feed.py:596`) and treats `oi == 0` as "unavailable", which is the right shape
  either way.
- `subscribe`/`unsubscribe` are **actions on an open socket**; sending them before
  `on_connect` closes the socket in `kiteconnect` 5.x (`ticker.py:568-585`) — this is
  exactly F2. **[verified by reading the installed SDK]**

### 4.2 Tick timestamps

- Kite sends `exchange_timestamp` (when the exchange stamped the tick) and, on some
  feeds, `last_trade_time`. Our code prefers `exchange_timestamp`, falls back to
  `last_trade_time`, then to receipt time (`live_feed.py:628-640`).
  **[to verify]** which fields are present in `quote` vs `full`, and whether
  `last_trade_time` can be stale for illiquid instruments.
- **Latency = receipt − exchange timestamp.** Values outside `[0, 300 s]` are discarded
  as clock skew (`live_feed.py:657-666`), and `_CLOCK_SKEW_WARN = 30` exists but is
  never used — the UI should warn, not silently print nothing (F24/F15).
- **Never drive candle placement from receipt time**: `_handle_tick` builds the candle
  from `received_at`, not `tick.exchange_ts` (`live_feed.py:678-686`). That is a real
  deviation from the bot's builder (which uses tick time). Recorded as a P0 follow-up
  inside F19's fix; the chart's candles must use exchange time to match `/api/chart/candles`
  after a backfill.

### 4.3 Historical data, rate limits, error mapping

- Historical candles exist for minute/3/5/…/day intervals and include OI
  **[cited]** <https://kite.trade/docs/connect/v3/historical>.
- **Exact request-per-second limit and the max days per minute request are *to verify***
  on that page (commonly quoted as ~3 req/s and 60 days/minute; do not encode the number
  from memory). P0's backfill uses `data/history.py`, which already has retry/backoff;
  P3 records the verified limits in `docs/data-sources.md` with the citation.
- **Error mapping** (`ui/kite_connect.py:_classify` and `core/errors.classify_exception`)
  must map: 403/token → `token_expired`; 401 → `token_expired`; 429 → `rate` with backoff;
  connection/timeout → `network`; `PermissionException`/no-plan → `no_plan`; anything else
  → generic. Websocket close codes: `1004`/auth wording → `token_expired`; `1006`/abnormal
  → reconnect; explicit `close()` → `disconnected`. **[to verify]** the code numbers
  against the SDK/docs before shipping the mapping table.

### 4.4 Access-token expiry

- The access token expires at **06:00 IST the next day** (regulatory) unless invalidated.
  **[cited]** <https://kite.trade/docs/connect/v3/user>.
- Therefore: the UI's "Connected as <name>, expires ~06:00" is correct; a *forced*
  reconnect is only needed once per morning; and the feed must treat an auth error as
  terminal-until-reconnected rather than retrying forever (already implemented).

### 4.5 Instrument master and contract rollover

- Tokens for derivatives are per-contract and **change at expiry**; the instrument dump
  must be cached daily and refreshed after 08:30 IST. **[to verify]** against
  <https://kite.trade/docs/connect/v3/market-quotes> (instrument dump section).
- Alias resolution (`NFO:NIFTY-FUT` → nearest-expiry contract) belongs in
  `data/instruments.py`; the dashboard must never invent a token. P0 adds
  `resolve_symbol → token` via that module, with a warning when the resolved contract
  expires within 2 trading days, and a mid-session rollover check (re-resolve on each
  backfill). **[to verify]** the expiry-date field name in the dump.

### 4.6 NSE sessions

- **Pre-open is 09:00–09:15** with sub-phases: order entry 09:00–09:10 (random close in
  the last 2 minutes), matching 09:10–09:12, buffer 09:12–09:15. **[cited]**
  <https://www.nseindia.com/static/products-services/equity-market-pre-open> and the
  derivatives page <https://www.nseindia.com/static/products-services/equity-derivatives-pre-open-session>.
- **Normal market 09:15–15:30; closing session 15:40–16:00; trade modification to 16:15.**
  **[cited]** <https://www.nseindia.com/static/market-data/market-timings>.
- **Which windows are tradable for alerts?** Only **09:15–15:30** (the continuous session).
  Pre-open has no continuous prices (the opening price is an auction), the closing session
  is a separate call auction, and neither feeds a 1-minute candle series that ORB/EMA/RSI
  were designed for. Pre-open and closing are **chart context only** and must be labelled
  as such. Justification: the strategy's own warm-up and ORB window are defined inside the
  continuous session, and alerting off auction prints would be indefensible.
- **Muhurat and special Saturday sessions** are announced ad hoc by NSE; model them as
  explicit, dated config exceptions (a `special_sessions:` block) — never as a rule.
  **[to verify]** dates each year from the official circular.
- **Circuit-breaker halts**: no special handling is possible from tick data alone; the
  watchdog already covers "no ticks". Document it as a known limitation.

### 4.7 Warm-up, ORB, and the first minutes

- The engine seeds from history (`engine.seed`) — the bot uses 4 warm-up days
  (`warmup_days=4`), so EMA/RSI/MACD are warm before 09:15. **The chart feed has no
  equivalent until F1's backfill is wired**, which is why the chart's indicators can look
  "unwarmed" at the open today.
- **ORB** uses the first `ORB_MINUTES` of the session (`config/defaults.SESSION_MINUTES`),
  so no ORB rule can possibly agree before 09:15 + ORB_MINUTES. Alerts in the first
  minutes therefore rely on the other four rules; that is by design, not a bug.
- Documented consequence (no rule change): an alert at 09:16 is structurally weaker than
  one at 10:00, and the UI should say so next to the check marks.

### 4.8 Open-interest cadence

- OI updates less frequently than price (it changes when positions change, and the feed's
  OI field can lag by seconds to minutes). **[to verify]** against the mode table and a
  live session.
- Consequence: the OI rule lags; a BUY can fire on price while OI has not yet refreshed.
  The UI must show "OI refreshes less frequently than price" (it already does in one
  panel) and the alert reason must never imply a fresh OI print when the value is stale.

### 4.9 Data that must never leak into signals

Delayed NSE, crypto and simulated ticks are chart-only. Enforced today by construction
(the chart has its own `CandleBuilder`, never the engine or the notifier). P0 must keep
that boundary and add a test: `test_chart_feed_never_calls_engine_or_hub` (monkeypatch
both to raise and drive a tick through the chart path).
---

## 5. Phased plan

Rules for every phase: branch from latest `main` named `fix/p<N>-<slug>`, **one phase per
branch**, failing test first (METHOD in §6), all CI jobs green, PR opened and never merged,
report includes diff summary + before/after evidence + UNVERIFIED list.

### P0 — real live feed (F1 + F2 + F5 wording, plus F17/F19/F20/F24 as required by the fix)

Ordered checkpoints (each is a commit with green tests):

1. **Characterization** of today's behaviour with `FakeTicker`: record that no ticker is
   ever created (F1) and that `subscribe` precedes `on_connect` (F2). Two failing tests
   committed first; PR body carries their output.
2. **State machine** (`live_feed/state.py` or in place): single `FeedState`, transitions,
   `availability()` resolver; `status()` derived only from it; `STATE_NO_PLAN` assignable.
3. **Transport**: queue subscriptions, flush in `on_connect`/`on_reconnect`, generation
   counter, `pause()` vs `stop()`, reconnect backoff (or documented delegation), honest
   `subscribed` count.
4. **Wiring**: `FeedController.ensure_started()` + symbol→token resolution + bootstrap;
   called from connect, reload and chart symbol selection.
5. **Surfaces**: `data_sources`/`source_info`/`candles`/`stream`/JS picker all read
   `availability()`; delete every "later update (C4)" string.
6. **SSE**: non-blocking drain (`asyncio.to_thread`), cap enforced under lock (F17).
7. **Candle time**: use exchange timestamp for charts (F19).

Files: `ui/live_feed.py`, `ui/app.py`, `ui/chart/service.py`, `ui/chart/sources.py`,
`ui/kite_connect.py`, `ui/static/chart.js`, `tests/ui/test_live_feed_*.py`,
`tests/ui/test_feed_state_machine.py`, `docs/live-feed.md`.

Acceptance: the F1 script (connected user, no simulator) returns candles and tick events;
the F2 script shows `exchange_subscribed=[...]` and `socket_closed=False`; grep finds no C4
string; every surface agrees per state; health latency < 100 ms with 5 streams open.

Commands: `pytest --cov=kite_alerts -q`, `pytest tests/ui -q -k "feed or stream"`,
`ruff check`, `black --check`, `mypy`, plus the two repro scripts pasted in the PR.

Risk: highest of all phases (touches the connect path). Rollback: revert the branch; the
old behaviour is "feature off", not data loss. Mitigation: everything behind `FakeTicker`,
no real network in tests.

### P1 — calendar (F3 + F4)

1. `describe()` → explicit states incl. pre-open sub-phases, closing, before-open with
   "opens 09:15".
2. `special_sessions:` config block (dated overrides) + validation.
3. Holiday handling: `scripts/refresh_holidays.py`, startup warning when empty/stale,
   About-page display. Ship official dates only if verifiable.
4. UI: session text from one endpoint; no component invents a label.

Files: `core/calendar.py`, `config/settings.py`, `config/holidays.yaml`,
`scripts/refresh_holidays.py`, `ui/status.py`, `ui/static/app.js`, tests
`tests/core/test_calendar_states.py`, `tests/core/test_calendar_holidays.py`.

Acceptance: exhaustive minute-of-day property test; holiday ⇒ no alerts + one warning;
a configured special session overrides the weekday rule.

Risk: a wrong holiday silences a session — mitigated by official sourcing + watchdog.
Rollback: config-only revert is enough to restore fail-open behaviour.

### P2 — security, messages, clock (F6 + F7 + F8 + F14)

1. CSRF token (cookie + header) for all mutations; keep Host/Origin; document `curl`.
2. `EnvFile` helper with a lock shared by `SettingsStore` and `write_access_token`
   (F26 folded in, because the CSRF work touches the same write path).
3. Absolute-path resolution anchored to the `.env` folder + startup logging + About page +
   mismatch warning (F14).
4. Nanosecond warning fix (F8).
5. Regression test that no settings message contains `.tmp` (F7).

Files: `ui/security.py`, `ui/settings_store.py`, `ui/app.py`, `cli/login_cmd.py`,
`config/settings.py`, `cli/run.py`, `ui/server.py`, `ui/static/*.js`, tests.

Acceptance: mutation without token → 403; same `.env` from two `cwd`s → identical resolved
paths; no `UserWarning` under `-W error::UserWarning`; no `.tmp` in any message.

Risk: CSRF breaks the UI — the Playwright smoke test (P4) is the guard; land the token with
a one-release "warn-only" mode if needed. Rollback: revert the branch.

**Status: closed out on `fix/p2-closeout` (PR #22 + #24 merged).** All five items are
implemented and tested. The first pass, audited against this plan, had four gaps which
the close-out branch fixes (each with a test that fails on `bf7fb25`):

1. F18's documented overflow policy was not the implemented one (a full queue let an
   incoming `system` message evict an alert), dropped alerts were not written to the
   dead-letter log, and the 10k-backlog test from the plan was absent. See §2.18.
2. F6's `curl` handshake existed only in the PR body — now in README and pinned by
   `test_curl_handshake_is_documented`; the duplicated token logic in `index()`/`about()`
   and the dead `csrf_token_for` helper were consolidated into
   `resolve_csrf()`/`set_csrf_cookie()`. See §2.6.
3. Two tests were accidentally deleted by the P2 edit rather than kept:
   `TestRedaction::test_redact_removes_tokens_and_webhooks` and
   `TestHolidayLoading::test_invalid_yaml_raises`. Both restored unchanged.
4. `_offer` evicted a message even when the consumer had just freed a slot (extra drop,
   inconsistent `dlq.count`). The enqueue and the eviction decision now share the
   queue mutex; see §2.18.

AST audit over every P2-touched source file (`config/settings.py`, `core/clock.py`,
`notifiers/hub.py`, `ui/app.py`, `ui/security.py`, `ui/server.py`,
`ui/settings_store.py`, `cli/login_cmd.py`, `cli/run.py`) found **no function or class
removed** relative to `e2bcc41^` (pre-P2). Relative to the merged P2 code, the never-called
`csrf_token_for` helper was replaced by `resolve_csrf`/`set_csrf_cookie`, and the
now-unused `secrets`/`CSRF_COOKIE_NAME` imports were dropped.

### P3 - release + CI + sources + repo hygiene (F9 + F10 + F16 + F23 + F25)
### P2b — concurrency hardening (F21 + F22)

1. `ChartService`: one re-entrant lock around the watchlist read-modify-write **and**
   the reads (Windows: a reader holding the file open makes another thread's
   `os.replace` fail with "Access is denied"); per-writer temp names (`pid` +
   `uuid4`); remove the temp file if the write fails.
2. `LiveFeedService`: move `_last_push` into the existing tick lock; take `_lock` for
   `_bootstrap_data` writes and return a copy from `bootstrap_data_for`; document
   `_lock → _sse_lock → builder._lock`.
3. Correct F22's stale `_registry` claim (it has been internally locked all along) and
   F21's "20 symbols" acceptance (the product caps the list at `WATCHLIST_LIMIT = 10`).

Files: `ui/chart/service.py`, `ui/live_feed.py`,
`tests/unit/test_chart_watchlist_concurrency.py`,
`tests/unit/test_live_feed_concurrency.py`.

Acceptance: every concurrent add within the cap survives, each writer has a unique temp
file, no stray `.tmp`; a reader cannot mutate the bootstrap cache; 4 ticker + 2 reader
threads run clean; the lock order is documented.

Risk: local, defensive; revert the branch.

### P3 — release + CI + sources (F9 + F10 + F16)

1. `workflow_dispatch` dry-run for release; guard every publish step.
2. Python 3.13/3.14 as `continue-on-error`; update classifiers/README.
3. `scripts/smoke_sources.py` (manual) + provider terms/limits in `docs/data-sources.md`.
4. `.gitattributes` with `* text=auto eol=lf` so a Windows checkout matches the LF golden
   file; renormalise the index and prove the diff is endings-only.
5. Calendar cache keyed by session values + YAML file stamps, never `id(settings)`.

Acceptance: a dispatch builds/passes artifacts and provably skips the Release; the matrix
runs 3.10-3.14 with 3.13/3.14 non-blocking; the smoke script prints lag and exits non-zero
on schema drift; `git check-attr eol demo_output.txt` is `lf`; equal settings reuse one
calendar while changed values or an edited holiday file rebuild it.

Risk: none to the product. Rollback: revert workflows/config; the code changes are local.

**Landed (P3).** All five items are on this branch. Offline verification:
`tests/test_workflows.py` (5 tests), `tests/unit/test_repo_hygiene.py` (3),
`tests/unit/test_chart_calendar_cache.py` (3),
`tests/unit/test_smoke_sources.py` (7) - 14 of those 18 fail on `main` for the
expected reasons (missing dry-run input/guard, no 3.13/3.14 matrix rows, no
`.gitattributes`, identity-keyed calendar, missing smoke script). The manual
dry-run and the live provider run cannot be done from this machine; both are
UNVERIFIED in the PR.

### P4 — tests for the product path + frontend (F11 + F15)

1. Fault-injection tests for `cli/run.py`, `cli/main.py`, `engine/worker.py` → **≥88%** each.
2. `node --test` unit tests for pure JS logic; Playwright smoke in CI against
   `--simulate-kite`, light **and** dark.
3. Assertions: SIMULATED never renders LIVE; token expiry falls back to Delayed with a
   message; no console errors on any page.

Acceptance: coverage thresholds met and enforced per module; the two UI assertions fail if
the state machine lies.

Risk: Playwright adds CI time and a browser download — cache it, keep it non-blocking for
one release. Rollback: drop the job.

**Status: implemented on `fix/p4-product-path` (this branch).** F11 fault tests, the three
pure JS modules + 19 Node tests, and 7 Playwright smoke tests are in place. The
per-module coverage thresholds were verified from the full `--cov=kite_alerts` report
(`worker.py` 93%, `cli/run.py` 91%, `cli/main.py` 93%). The separate
`--cov=kite_alerts.cli.run` form corrupts pandas on this Python 3.14 machine, so the
CI gate remains the package-wide `--cov=kite_alerts` run (see PR UNVERIFIED).

### P5 — behaviour-preserving refactor (F12)

Split `live_feed.py`, `app.py`, `chart/service.py`; add characterization tests **before**
each move (recorded JSON payloads). No behaviour change allowed; coverage must not drop.

Rollback: the branch is a pure move — revert restores the monolith.

**Status: partial on `fix/p5-refactor` (this branch).** The `live_feed.py` value/bootstrap
extraction is complete and characterized; `app.py`, `chart/service.py` and `settings_schema.py`
are untouched. Public imports unchanged, coverage not lower (92.76%).

### P6 — documentation + AGENTS.md (F13)

1. `AGENTS.md` with the six hard rules (alerts-only, no secrets, never change signals,
   never mix data sources, one phase per branch/PR, state what is unverified) plus the
   repo commands.
2. Correct `docs/ROADMAP.md` (phase history, no Docker, "planned" markers), fix
   `docs/live-feed.md` claims, add the live-session checklist.
3. Docs test forbidding stale C4 strings and unqualified Docker mentions.

Rollback: docs only.
---

## 6. Test strategy

### 6.1 METHOD (mandatory, per bug)

1. Write a test that **fails on current `main`**, run it, paste the failure into the PR.
2. Fix.
3. Show the same test passing, plus the full suite.
4. No fix without a reproducing test; no test that only asserts "did not raise".

### 6.2 Deterministic concurrency

- `threading.Event` / `threading.Barrier` for interleavings, **never** `time.sleep` to
  "wait for" a thread. Where a delay is unavoidable (e.g. proving backoff gaps), inject a
  fake clock instead.
- A `FakeTicker` with fault injection: delayed `on_connect`, dropped socket, reconnect,
  duplicate tick, out-of-order tick, missing OI, cumulative-volume reset, auth error, and
  **`subscribe()` raising before `on_connect` exactly like `kiteconnect` 5.x** (F2).
- Every test that touches the feed asserts the **state machine** transition, not just the
  return value.

### 6.3 Contracts

Fixture payloads built from the official docs (one tick per mode: `ltp`, `quote`, `full`;
one instrument-dump row; one historical response) so a field rename in the SDK or a wrong
assumption in our parser fails a test rather than production.

### 6.4 Soak / stress (marked `slow`, not on every PR)

Many SSE clients (60), tick bursts (10k/s for 10 s), repeated start/pause/rebuild (50×),
asserting: bounded memory (RSS growth < 50 MB), no thread leak (`threading.active_count()`
returns to baseline), no deadlock (whole run under a watchdog timeout), queue drops counted.

### 6.5 Property tests

- Candle merging: for any tick sequence, closed candles are non-overlapping, monotonic,
  `low ≤ open/close ≤ high`, volume ≥ 0 and equals the cumulative delta when no reset.
- Resampling: 1m → 3/5/15m is idempotent under re-sampling of an already-resampled series
  and preserves OHLC invariants.
- Calendar: every minute maps to exactly one state; `in_session` is a subset of `open`.

### 6.6 Coverage

Keep `fail_under = 85` overall, and add per-module gates in CI for the three F11 targets
(≥88%) so a later refactor cannot regress them. Coverage is a floor, not a goal — the
suites above are the actual guarantee.

---

## 7. Observability and operations

### 7.1 Logs (names that must exist)

| Event | Level | Fields | User sees |
|---|---|---|---|
| `feed state change` | INFO | `from`, `to`, `reason`, `generation` | badge + status line |
| `feed connected` | INFO | `subscribed`, `flushed`, `latency_ms` | badge green |
| `subscribe deferred` / `flush failed` | WARNING | `tokens`, `error_kind` | "Could not subscribe, retrying" |
| `reconnect scheduled` | INFO | `attempt`, `delay_s` | "Retrying in Ns" |
| `backfill` | INFO | `symbol`, `days`, `bars`, `ms` | chart fills in |
| `token expired` | WARNING | `source` (`ticker`/`rest`) | red badge + Reconnect |
| `queue drop` | WARNING | `queue`, `dropped_total`, `policy` | (internal) |
| `settings saved` | INFO | `keys` (never values), `backup` | "Saved" |
| `paths resolved` | INFO | `env_file`, `db_path`, `log_dir` (absolute) | About page |

Secrets never appear: `redact()` already strips webhook URLs and tokens; the new logs must
route through it.

### 7.2 Metrics (Prometheus names)

`feed_state{state}` (gauge), `feed_reconnects_total`, `feed_subscribe_failures_total`,
`feed_backoff_seconds`, `feed_ticks_total{symbol}`, `feed_tick_latency_ms` (histogram),
`backfill_bars_total{symbol}`, `backfill_failures_total{reason}`, `sse_clients` (gauge),
`sse_dropped_total`, `sse_queue_depth`, `notifier_queue_depth`, `notifier_dropped_total`,
`tick_queue_depth`, `ticks_dropped_total`, `worker_errors_total`, `auth_errors_total`.

### 7.3 Runbook (short)

1. **Badge amber "Connecting…" > 30 s** → check network; check token age; look for
   `subscribe deferred`. Restart the dashboard if `pause/rebuild` failed.
2. **Badge green, no candles** → `feed_subscribe_failures_total` > 0 means F2 regressed;
   `backfill_failures_total{reason="auth"}` means re-login.
3. **Badge "Token expired"** → Connect page, re-login; the chart falls back to Delayed.
4. **"Stale since HH:MM:SS"** during market hours → upstream feed problem; the bot's
   watchdog will alert independently.
5. **Dashboard slow** → check `sse_clients` and event-loop blocking (F17); reduce open tabs.
6. **Two databases?** → check the `paths resolved` log line (F14) before anything else.

---

## 8. Risks, and what cannot be verified offline

### 8.1 What is UNVERIFIED (needs a paid plan and a live market)

- [ ] A real `KiteTicker` handshake, `on_connect` ordering and the **flush-on-connect**
      fix against the real SDK and real frames.
- [ ] Whether a **second `KiteTicker` on the same running reactor** works after a token
      rotation (design §3e) — the single riskiest assumption in P0.
- [ ] Real subscription-mode fields (OI only in `full`, volume semantics) and real
      OI refresh cadence.
- [ ] Historical API rate limits and the max days per minute request (§4.3) — **[to verify]**
      on the official page before encoding numbers.
- [ ] Reconnect behaviour against a real network drop, and whether Kite's own reconnect
      fights ours.
- [ ] Contract rollover during a session, and the instrument-dump expiry field.
- [ ] Real Telegram/Slack/SMTP delivery (including proxy/DPDI networks).
- [ ] Windows firewall behaviour on first `kite-alerts ui` bind (and whether the user gets
      an OS prompt on 127.0.0.1 — it should not, but that is unverified).
- [ ] Browser appearance, accessibility (contrast, keyboard, screen reader) and layout on
      the owner's machine and resolution.
- [ ] Delayed NSE (`yfinance`) and crypto (Kraken) adapters against live endpoints
      (F16 script exists only after P3).
- [ ] Latency/clock skew on the owner's PC.
- [ ] The official NSE holiday fetch (N4): TLS connects, the read times out for
      scripted clients — `scripts/refresh_holidays.py` must be run once by the
      owner against the live page (or with a saved `--html` page) and its diff
      reviewed. Until then `config/holidays.yaml` stays empty and the startup
      warning stays on.

### 8.2 Risks of this plan

| Risk | Mitigation |
|---|---|
| P0 touches login + feed; a regression stops chart *and* connect | Land in checkpoints 1–7 with the suite green at each; keep `ensure_started()` a no-op without credentials; Playwright smoke before merge |
| Twisted singleton cannot restart → pause/rebuild may not work | Fallback: terminal `stop()` + explicit "restart the dashboard" message; subprocess design deferred behind the same interface |
| CSRF (P2) breaks automation | one-release warn-only mode + documented token path for `curl` |
| Refactor (P5) shifts behaviour | characterization tests before any move; coverage gate per module |
| Line-ending renormalisation (F23) creates a huge diff | separate commit, `git add --renormalize`, verify tree identical apart from endings |
| Holiday list wrong (P1) | official sourcing only; watchdog remains the backstop |

### 8.3 Manual acceptance checklist (owner, live session)

1. Fresh morning: `kite-alerts login` → "Connected as <name>, expires about 06:00 IST".
2. `kite-alerts ui` → Chart tab → **Kite live** selected → badge turns **LIVE** within
   10 s of the first tick; `feed.subscribed` equals the chart symbol + watchlist.
3. Kill the network for 20 s → badge shows retry countdown → recovers to **LIVE** without a
   dashboard restart; the gap is backfilled (no duplicate candles in the chart).
4. Token expiry simulation (dev control) → badge **Token expired**, chart falls back to
   Delayed with a message, Connect page re-establishes LIVE.
5. Futures symbol → OI panel present, note "OI refreshes less frequently than price".
6. `kite-alerts run` in a second window from a **different** folder after P2 → About page
   shows the same absolute DB/log paths and a warning appears if not.
7. Send test alert → phone buzzes; history does not gain a row.
8. Leave both running through 15:30 → status moves to closing/closed; no alert storms.
9. Restart the PC → both commands start clean, no stale PID, no duplicate connections.
10. Confirm no `.env`/token value appears in `logs/` (grep the log dir).

---

## 9. Definition of done

**Per phase:** failing-then-passing test evidence in the PR; full suite green; `ruff`,
`black --check`, `mypy` clean; coverage ≥85% overall and ≥88% for touched product modules;
no secrets in diff/logs; PR open (never merged); UNVERIFIED list updated.

**Whole effort (P0–P6):**

1. A connected user with a paid plan sees **LIVE** only when ticks flow, and candles render
   from the live feed — proven by the F1 script and by the manual checklist.
2. `subscribe` is never sent before `on_connect`; a subscribe failure can never display
   LIVE — proven by tests that fail on today's `main`.
3. Exactly one source of truth for availability; no "later update (C4)" string anywhere.
4. Calendar states are exhaustive and honest; holidays are official or loudly warned about.
5. All mutations require a CSRF token; no `.env` write loses data; paths are absolute and
   identical for bot and dashboard regardless of `cwd`.
6. CI covers 3.10–3.14; the release workflow has a passing dry-run; frontend logic has
   unit + Playwright coverage with badge-truthfulness assertions.
7. ≥88% on `cli/run.py`, `cli/main.py`, `engine/worker.py`.
8. `AGENTS.md` exists and the six hard rules are enforced (alerts-only scan still green).
9. `IMPLEMENTATION.md` (this file) is updated with what actually shipped, and every
   remaining item is either closed or listed as UNVERIFIED with a reason.
