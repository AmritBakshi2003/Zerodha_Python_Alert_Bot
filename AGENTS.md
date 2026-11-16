# AGENTS.md — working rules for agents (and humans) in this repo

This file exists so any coding agent can work here without re-learning the
rules the hard way. The full audit and phase plan lives in `IMPLEMENTATION.md`.

## Hard rules (never violated, not even "temporarily")

1. **Alerts only — no order / trade / position / holdings / GTT code, ever.**
   The bot evaluates market data and writes diagnostic alerts. It never places,
   modifies or cancels orders and never touches positions or holdings.
   `tests/test_alerts_only.py` enforces this mechanically: it scans every
   `.py`, `.js` and `.html` file for Kite order-side method names and fails the
   build if any appear. Keep it green in every commit.
2. **Do not change signal rules or evaluator semantics.** If you suspect a bug
   in a rule, write a test that documents the current behaviour, note the
   suspected bug in the PR, and ask. Never "fix" a rule silently.
3. **Delayed, crypto and simulated data never reach the notifier, the alert
   store or the alert history.** They may only feed the chart and the demo
   surfaces, clearly labelled (DELAYED / SHOWCASE / DEMO / SIMULATED).
4. **No secrets in git or in logs.** API keys, secrets and access tokens live
   in `.env` (never committed) and are never logged. Tests assert markers do
   not leak into responses.
5. **Free and open source.** No paid service, no hosted proxy, no telemetry.
   BYOK: users run their own Kite Connect app.
6. **Every bugfix starts with a failing test.** Write the test, run it on the
   base commit, paste the failing output into the PR, then fix, then show the
   passing output. A fix without a reproducing test does not ship.
7. **One phase per branch.** Create `fix/<phase-name>` from `main`, do exactly
   one phase's scope, open a PR against `main`, and STOP. Never push to
   `main`, never merge, never start the next phase until the user replies
   `approve <phase>`.
8. **All CI jobs stay green.** Full pytest suite, coverage floor, ruff check +
   format, mypy strict, and the alerts-only scanner.
9. **Say what you did not verify.** Anything not executed on this machine goes
   into an UNVERIFIED list in the PR. Never claim a test, lint or tool run
   that did not happen. Never invent data (e.g. NSE holiday dates).

## Concurrency and determinism

* Tests must be deterministic: use `threading.Event`, `threading.Barrier`,
  injectable clocks (`FakeMonotonic`, `FrozenClock`) and fakes with explicit
  triggers (`FakeTicker.open_socket()`, `push()`, `error()`). Never assert on
  wall-clock timing; never `time.sleep()` to synchronise.

## One-phase workflow

1. Read the phase's scope in `IMPLEMENTATION.md` (sections "Phased plan" and
   "Findings"). Verify `git status` is clean and you are on a fresh branch
   created from `origin/main`.
2. Reproduce every finding with a test that fails on `main`; save the output.
3. Commit the failing tests first, then the fix, then run:
   * `py -3 -m pytest --cov=kite_alerts -q` (full suite, coverage floor 85%)
   * `py -3 -m ruff check src tests && py -3 -m ruff format --check src tests`
   * `py -3 -m black --check src tests scripts` (CI runs all three dirs;
     black and ruff format can disagree — make the code satisfy both)
   * `py -3 -m mypy src/kite_alerts`
   * `py -3 -m pytest tests/test_alerts_only.py -q`
4. Update `IMPLEMENTATION.md` (status table + findings) as part of the phase.
5. Open the PR with: diff summary, failing-then-passing evidence per finding,
   test/coverage/lint/type numbers, and the UNVERIFIED list. STOP.

## Environment notes (Windows)

* Python 3.14 system interpreter (`py -3`), repo installed editable — there is
  no `.venv` in the working tree.
* `TestClient` must be built with `base_url="http://127.0.0.1:8765"`; the
  security middleware rejects Starlette's default `testserver` host.
* `tests/fakes/fake_kite.py` models the SDK's *shape*, including exception
  names (`TokenException`, `InputException`) because error classification is
  name-based. `FakeTicker.strict_socket = True` makes `connect()` asynchronous
  like the real SDK; call `open_socket()` to complete the handshake.
* Today's date: Mon Nov 16 2026
