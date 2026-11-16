"""P0.1 — every surface must tell the truth about the feed (N1, N2, F5).

Confirmed against ``main`` (fe30686) with a connected-user repro before this
branch changed anything; the repro output is pasted in the PR:

* **N1** after a mid-session token expiry, ``/api/chart/candles?source=kite_live``
  returned 200 with ``badge="LIVE"`` and the stale notice "Connecting to the
  Kite live ticker..."; the payload carried no feed state at all, and a second
  ticker was built with the same dead token.
* **N2** ``/api/data-sources`` cached the whole payload for ``DATA_TTL_SECONDS``
  (60 s), so the picker kept advertising the previous feed state for up to a
  minute after the feed changed.
* **F5** the plan probe, the feed state and the chart payloads could disagree
  and ``STATE_NO_PLAN`` was unreachable.

Every test in this module fails on that main; the fixes live in this branch.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from fakes.fake_kite import FakeKite, FakeTicker
from kite_alerts.core.clock import IST
from kite_alerts.ui.app import build_context, create_app
from kite_alerts.ui.feed_controller import FeedController
from kite_alerts.ui.kite_connect import DATA_TTL_SECONDS
from kite_alerts.ui.live_feed import (
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    STATE_LIVE,
    STATE_MARKET_CLOSED,
    STATE_NO_PLAN,
    STATE_SIMULATED,
    STATE_STALE,
    STATE_TOKEN_EXPIRED,
    LiveFeedService,
)
from unit.ui_support import BASE_URL, FakeMonotonic
from unit.ui_support_c2 import FakeKite as ProbeKite

TOKEN = 111
SYMBOL = "NSE:TEST"
CONNECTING_NOTICE = "Connecting to the Kite live ticker..."
LIVE_NOTICE = "Streaming live ticks from Kite."
EXPIRED_NOTICE = "Your Kite session expired. Reconnect to use live data."

ENV_TEXT = (
    "# a comment the writer must preserve\n"
    "KITE_API_KEY=KEY-MARKER-4f1a\n"
    "KITE_API_SECRET=SECRET-MARKER-9c2b\n"
    "KITE_ACCESS_TOKEN=ACCESS-TOKEN-MARKER-8899\n"
    "NOTIFY_CHANNELS=console\n"
    "SYMBOLS=NSE:TEST\n"
)


class PermissionException(Exception):
    """Same name as the SDK's, because probe classification is name-based."""


class TokenException(Exception):
    """Same name as the SDK's, because probe classification is name-based."""


@dataclass
class _ResolvedInstrument:
    symbol: str = SYMBOL
    token: int = TOKEN
    is_future: bool = False


def _stub_resolver_factory(api_key: str, access_token: str, cache_dir: Any) -> Any:
    return SimpleNamespace(resolve=lambda spec: _ResolvedInstrument())


def _push_tick(ticker: FakeTicker, *, price: float = 101.0) -> None:
    """One tick stamped inside the current IST minute (chart-only path)."""
    now = pd.Timestamp.now(tz=IST).tz_localize(None).to_pydatetime()
    ticker.push(
        [
            {
                "instrument_token": TOKEN,
                "last_price": price,
                "exchange_timestamp": now,
                "volume_traded": 10,
                "oi": 0.0,
            }
        ]
    )


@dataclass
class Harness:
    """A connected user: valid .env, a plan probe, a strict fake ticker."""

    client: TestClient
    context: Any
    env_file: Path
    probe: ProbeKite
    monotonic: FakeMonotonic

    @property
    def feed(self) -> LiveFeedService:
        return self.context.feed

    @property
    def ticker(self) -> FakeTicker:
        return FakeTicker.instances[-1]

    def live_entry(self) -> dict[str, Any]:
        """The picker's view of ``kite_live`` (plain GET, no refresh)."""
        body = self.client.get("/api/data-sources").json()
        return next(s for s in body["sources"] if s["id"] == "kite_live")

    def candle_payload(self) -> dict[str, Any]:
        response = self.client.get(
            "/api/chart/candles?source=kite_live&symbol=NSE:TEST&interval=1&limit=10"
        )
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture(autouse=True)
def _strict_fake_socket() -> object:
    """Model the real SDK everywhere in this module: connect() is async."""
    FakeTicker.strict_socket = True
    yield
    FakeTicker.strict_socket = False


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    FakeTicker.reset()
    FakeTicker.strict_socket = True
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_TEXT, encoding="utf-8")
    probe = ProbeKite()  # ltp succeeds: the account has a market-data plan
    monotonic = FakeMonotonic()
    context = build_context(
        env_file=env_file,
        kite_factory=lambda api_key, access_token="": probe,
        ticker_factory=lambda key, token: FakeTicker(key, token),
    )
    context.kite.monotonic = monotonic  # injectable clock for the probe cache
    context.feed_controller = FeedController(
        context.feed,
        lambda: context.settings,
        resolver_factory=_stub_resolver_factory,
    )
    client = TestClient(create_app(context), base_url=BASE_URL)
    bundle = Harness(client, context, env_file, probe, monotonic)
    try:
        with client:  # lifespan: ensure_started -> the feed is connecting
            yield bundle
    finally:
        FakeTicker.reset()
        FakeTicker.strict_socket = False


def _make_feed(*, is_simulated: bool = False) -> LiveFeedService:
    FakeTicker.reset()
    FakeTicker.strict_socket = True  # reset() clears it
    fake_kite = FakeKite()
    return LiveFeedService(
        ticker_factory=lambda key, token: FakeTicker(key, token),
        kite_factory=lambda: fake_kite,
        is_simulated=is_simulated,
    )


class TestConnectedUserPayloadTruth:
    """The acceptance run: start -> live -> token expiry, payload == picker."""

    def test_start_live_and_expiry_agree_on_every_surface(self, harness: Harness) -> None:
        # -- STEP 1: start -------------------------------------------------
        assert harness.feed.status().state == STATE_CONNECTING
        live = harness.live_entry()
        assert live["state"] == STATE_CONNECTING
        assert live["enabled"] is True
        assert live["badge"] == "CONNECTING"  # was "LIVE" on main (N1)
        assert live["notice"] == CONNECTING_NOTICE

        # No ticks yet: the candles endpoint must not pretend otherwise.
        waiting = harness.client.get(
            "/api/chart/candles?source=kite_live&symbol=NSE:TEST&interval=1"
        )
        assert waiting.status_code == 503

        # -- STEP 2: live --------------------------------------------------
        harness.ticker.open_socket()
        _push_tick(harness.ticker)
        payload = harness.candle_payload()
        assert payload["badge"] == "LIVE"
        assert payload["feed_state"] == STATE_LIVE
        assert payload["notice"] == LIVE_NOTICE

        live = harness.live_entry()
        assert (live["badge"], live["state"]) == ("LIVE", STATE_LIVE)
        assert live["enabled"] is True
        assert live["notice"] == LIVE_NOTICE

        # -- STEP 3: mid-session token expiry ------------------------------
        harness.ticker.error(403, "TokenException: token is invalid")
        assert harness.feed.status().state == STATE_TOKEN_EXPIRED

        # The picker must flip immediately: no 60 s lag (N2).
        live = harness.live_entry()
        assert live["state"] == STATE_TOKEN_EXPIRED
        assert live["enabled"] is False
        assert live["badge"] == "EXPIRED"
        assert live["notice"] == EXPIRED_NOTICE

        # The chart payload must carry the state and fall back to Delayed.
        expired = harness.client.get(
            "/api/chart/candles?source=kite_live&symbol=NSE:TEST&interval=1"
        )
        assert expired.status_code == 422
        body = expired.json()
        assert body["code"] == "source_disabled"
        assert "expired" in body["message"].lower()
        assert "Delayed" in body["message"]

        # A dead token must never buy a second ticker (N1).
        assert len(FakeTicker.instances) == 1

    def test_a_fresh_login_recovers_the_feed(self, harness: Harness) -> None:
        harness.ticker.error(403, "TokenException: token is invalid")
        assert harness.feed.status().state == STATE_TOKEN_EXPIRED

        # Re-login: the .env token changes and the context reloads.
        harness.env_file.write_text(
            ENV_TEXT.replace("ACCESS-TOKEN-MARKER-8899", "NEW-ACCESS-TOKEN-2468"),
            encoding="utf-8",
        )
        harness.context.reload()

        assert harness.feed.status().state == STATE_CONNECTING
        assert len(FakeTicker.instances) == 2
        live = harness.live_entry()
        assert live["state"] == STATE_CONNECTING
        assert live["enabled"] is True
        assert live["badge"] == "CONNECTING"


class TestNoSecondTickerForADeadToken:
    """N1: a chart poll must not erase token_expired by restarting the feed."""

    def test_start_is_refused_with_the_same_dead_credentials(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t1")
        FakeTicker.instances[-1].open_socket()
        feed.set_token_expired()
        assert feed.status().state == STATE_TOKEN_EXPIRED

        feed.start(api_key="k", access_token="t1")  # what a chart poll did on main
        assert feed.status().state == STATE_TOKEN_EXPIRED
        assert len(FakeTicker.instances) == 1

    def test_start_with_new_credentials_is_allowed(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t1")
        FakeTicker.instances[-1].open_socket()
        feed.set_token_expired()

        feed.start(api_key="k", access_token="t2")  # a fresh login
        assert feed.status().state == STATE_CONNECTING
        assert len(FakeTicker.instances) == 2

    def test_ensure_started_does_not_retry_a_terminal_state(self, tmp_path: Path) -> None:
        feed = _make_feed()
        settings = SimpleNamespace(
            kite=SimpleNamespace(api_key="k", token="t1"),
            symbols=(SYMBOL,),
            cache_dir=str(tmp_path / "cache"),
        )
        controller = FeedController(feed, lambda: settings, resolver_factory=_stub_resolver_factory)

        assert controller.ensure_started() is True
        assert feed.status().state == STATE_CONNECTING

        feed.set_token_expired()
        assert controller.ensure_started() is False  # was True on main
        assert feed.status().state == STATE_TOKEN_EXPIRED
        assert len(FakeTicker.instances) == 1

        settings.kite.token = "t2"  # a fresh login clears the terminal state
        assert controller.ensure_started() is True
        assert feed.status().state == STATE_CONNECTING
        assert len(FakeTicker.instances) == 2


class TestAvailabilityBadge:
    """The badge may only claim LIVE while real ticks are fresh (N1)."""

    @staticmethod
    def _feed_in_state(state: str) -> LiveFeedService:
        feed = _make_feed(is_simulated=(state == STATE_SIMULATED))
        if state in (STATE_CONNECTING, STATE_SIMULATED):
            feed.start(api_key="k", access_token="t")
        elif state == STATE_LIVE:
            feed.start(api_key="k", access_token="t")
            FakeTicker.instances[-1].open_socket()
        elif state in (STATE_STALE, STATE_MARKET_CLOSED):
            feed.start(api_key="k", access_token="t")
            FakeTicker.instances[-1].open_socket()
            feed._last_tick_time = pd.Timestamp.now(tz=IST).timestamp() - 120.0
            # Injected, never the real calendar: the two states must not
            # depend on the time of day this test runs at.
            feed._market_is_open = lambda: state == STATE_STALE  # type: ignore[method-assign]
        elif state == STATE_TOKEN_EXPIRED:
            feed.start(api_key="k", access_token="t")
            feed.set_token_expired()
        elif state == STATE_NO_PLAN:
            feed.start(api_key="k", access_token="t")
            feed.set_no_plan("Your Kite plan has no market data.")
        return feed

    @pytest.mark.parametrize(
        ("state", "expected_badge", "expected_enabled"),
        [
            (STATE_DISCONNECTED, "OFF", False),
            (STATE_CONNECTING, "CONNECTING", True),
            (STATE_LIVE, "LIVE", True),
            (STATE_STALE, "STALE", True),
            (STATE_MARKET_CLOSED, "CLOSED", True),
            (STATE_TOKEN_EXPIRED, "EXPIRED", False),
            (STATE_NO_PLAN, "NO PLAN", False),
            (STATE_SIMULATED, "SIMULATED", True),
        ],
    )
    def test_badge_and_enabled_per_state(
        self, state: str, expected_badge: str, expected_enabled: bool
    ) -> None:
        view = self._feed_in_state(state).availability()
        assert view.state == state
        assert view.badge == expected_badge
        assert view.enabled is expected_enabled

    @pytest.mark.parametrize(
        "state",
        [
            STATE_DISCONNECTED,
            STATE_CONNECTING,
            STATE_STALE,
            STATE_MARKET_CLOSED,
            STATE_TOKEN_EXPIRED,
            STATE_NO_PLAN,
        ],
    )
    def test_live_is_never_claimed_without_fresh_ticks(self, state: str) -> None:
        view = self._feed_in_state(state).availability()
        assert view.badge != "LIVE"


class TestPayloadCarriesFeedState:
    def test_candles_and_symbols_payloads_carry_the_feed_state(self, harness: Harness) -> None:
        symbols = harness.client.get("/api/chart/symbols?source=kite_live").json()
        assert symbols["feed_state"] == STATE_CONNECTING

        harness.ticker.open_socket()
        _push_tick(harness.ticker)
        payload = harness.candle_payload()
        assert payload["feed_state"] == STATE_LIVE

        harness.ticker.error(403, "TokenException: token is invalid")
        symbols = harness.client.get("/api/chart/symbols?source=kite_live").json()
        assert symbols["feed_state"] == STATE_TOKEN_EXPIRED


class TestPickerRecomputeLive:
    """N2: cache only the plan probe; recompute the feed view on every call."""

    def test_data_sources_reflects_the_feed_immediately(self, harness: Harness) -> None:
        first = harness.live_entry()
        assert first["state"] == STATE_CONNECTING
        assert harness.probe.calls.count("ltp") == 1

        harness.ticker.open_socket()
        second = harness.live_entry()
        assert second["state"] == STATE_LIVE  # on main this still said connecting
        assert second["badge"] == "LIVE"
        assert harness.probe.calls.count("ltp") == 1  # the probe itself stays cached

        harness.ticker.error(403, "TokenException: token expired")
        third = harness.live_entry()
        assert third["state"] == STATE_TOKEN_EXPIRED
        assert third["badge"] == "EXPIRED"
        assert third["enabled"] is False
        assert harness.probe.calls.count("ltp") == 1

        # The plan probe keeps its TTL: it re-runs only after the cache expires.
        harness.monotonic.advance(DATA_TTL_SECONDS + 1)
        harness.live_entry()
        assert harness.probe.calls.count("ltp") == 2

    def test_refresh_bypasses_only_the_probe_cache(self, harness: Harness) -> None:
        harness.live_entry()
        assert harness.probe.calls.count("ltp") == 1
        harness.client.get("/api/data-sources?refresh=true")
        assert harness.probe.calls.count("ltp") == 2


class TestProbeDrivesTheFeedState:
    """F5: the plan probe owns STATE_NO_PLAN / token_expired when it detects them."""

    def _build_with_probe(
        self, tmp_path: Path, probe: ProbeKite
    ) -> tuple[TestClient, Any, FakeMonotonic]:
        FakeTicker.reset()
        FakeTicker.strict_socket = True
        env_file = tmp_path / ".env"
        env_file.write_text(ENV_TEXT, encoding="utf-8")
        monotonic = FakeMonotonic()
        context = build_context(
            env_file=env_file,
            kite_factory=lambda api_key, access_token="": probe,
            ticker_factory=lambda key, token: FakeTicker(key, token),
        )
        context.kite.monotonic = monotonic
        context.feed_controller = FeedController(
            context.feed, lambda: context.settings, resolver_factory=_stub_resolver_factory
        )
        client = TestClient(create_app(context), base_url=BASE_URL)
        return client, context, monotonic

    def test_a_permission_probe_sets_no_plan_and_success_clears_it(self, tmp_path: Path) -> None:
        probe = ProbeKite(ltp_error=PermissionException("insufficient permission"))
        client, context, monotonic = self._build_with_probe(tmp_path, probe)
        try:
            with client:
                live = next(
                    s
                    for s in client.get("/api/data-sources").json()["sources"]
                    if s["id"] == "kite_live"
                )
                assert context.feed.status().state == STATE_NO_PLAN  # unreachable on main
                assert live["state"] == STATE_NO_PLAN
                assert live["enabled"] is False
                assert "no market data" in live["notice"].lower()

                # The plan comes back: the next probe must release the feed.
                probe.ltp_error = None
                monotonic.advance(DATA_TTL_SECONDS + 1)
                released = next(
                    s
                    for s in client.get("/api/data-sources").json()["sources"]
                    if s["id"] == "kite_live"
                )
                assert context.feed.status().state == STATE_DISCONNECTED
                assert released["state"] == STATE_DISCONNECTED
        finally:
            FakeTicker.reset()
            FakeTicker.strict_socket = False

    def test_a_token_probe_sets_token_expired(self, tmp_path: Path) -> None:
        probe = ProbeKite(ltp_error=TokenException("token expired"))
        client, context, _monotonic = self._build_with_probe(tmp_path, probe)
        try:
            with client:
                live = next(
                    s
                    for s in client.get("/api/data-sources").json()["sources"]
                    if s["id"] == "kite_live"
                )
                assert context.feed.status().state == STATE_TOKEN_EXPIRED
                assert live["state"] == STATE_TOKEN_EXPIRED
                assert live["badge"] == "EXPIRED"
                assert live["enabled"] is False
        finally:
            FakeTicker.reset()
            FakeTicker.strict_socket = False


class TestAllSurfacesAgree:
    """F5: data-sources, the chart payload and the picker tell one story."""

    def test_payload_and_picker_agree_through_the_feed_states(self, harness: Harness) -> None:
        def agree(expected_state: str, *, payload: dict[str, Any] | None = None) -> None:
            live = harness.live_entry()
            assert live["state"] == expected_state
            if payload is not None:
                assert payload["feed_state"] == live["state"]
                assert payload["badge"] == live["badge"]
                assert payload["notice"] == live["notice"]
            else:
                symbols = harness.client.get("/api/chart/symbols?source=kite_live").json()
                assert symbols["feed_state"] == live["state"]
                assert symbols["badge"] == live["badge"]
                assert symbols["notice"] == live["notice"]

        agree(STATE_CONNECTING)  # symbols vs picker
        harness.ticker.open_socket()
        _push_tick(harness.ticker)
        # Wait for the feed worker to ingest the tick (bounded, deterministic
        # bound) before asserting the chart payload agrees.
        deadline = time.monotonic() + 5.0
        payload: dict[str, Any] | None = None
        while payload is None:
            response = harness.client.get(
                "/api/chart/candles?source=kite_live&symbol=NSE:TEST&interval=1&limit=10"
            )
            if response.status_code == 200:
                payload = response.json()
                break
            if time.monotonic() >= deadline:
                raise AssertionError(response.text)
            time.sleep(0.05)
        agree(STATE_LIVE, payload=payload)

        # Stale: no tick for a while, market still open (injected: the real
        # calendar is time-of-day dependent and must not decide this test).
        harness.feed._last_tick_time = pd.Timestamp.now(tz=IST).timestamp() - 120.0
        harness.feed._market_is_open = lambda: True  # type: ignore[method-assign]
        agree(STATE_STALE, payload=harness.candle_payload())

        # Market closed: same staleness, but the session is over.
        harness.feed._market_is_open = lambda: False  # type: ignore[method-assign]
        agree(STATE_MARKET_CLOSED, payload=harness.candle_payload())

        # Token expiry: the picker and the 422 message must agree on the reason.
        # Restore the market-open assumption the market-closed step overrode, so
        # the token-expired transition is evaluated in an open-market context.
        harness.feed._market_is_open = lambda: True  # type: ignore[method-assign]
        harness.ticker.error(403, "TokenException: token is invalid")
        deadline = time.monotonic() + 5.0
        live: dict[str, Any] | None = None
        while live is None or live.get("state") != STATE_TOKEN_EXPIRED:
            feed_state = harness.feed.status().state
            live = harness.live_entry()
            if feed_state == STATE_TOKEN_EXPIRED or live.get("state") == STATE_TOKEN_EXPIRED:
                break
            if time.monotonic() >= deadline:
                msg = (
                    f"expected {STATE_TOKEN_EXPIRED}, got feed={feed_state} "
                    f"live={live.get('state')}"
                )
                raise AssertionError(msg)
            time.sleep(0.05)
        expired = harness.client.get(
            "/api/chart/candles?source=kite_live&symbol=NSE:TEST&interval=1"
        )
        assert expired.status_code == 422
        assert live["notice"] in expired.json()["message"]

        # No plan: the probe discovers it while the ticker is not running.
        harness.probe.ltp_error = PermissionException("insufficient permission")
        harness.monotonic.advance(DATA_TTL_SECONDS + 1)
        live = harness.live_entry()
        assert live["state"] == STATE_NO_PLAN
        blocked = harness.client.get(
            "/api/chart/candles?source=kite_live&symbol=NSE:TEST&interval=1"
        )
        assert blocked.status_code == 422
        assert live["notice"] in blocked.json()["message"]
