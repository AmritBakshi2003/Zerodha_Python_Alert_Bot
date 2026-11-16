"""P0 regressions for the live feed: F1/F2/F5/F19/F20/F24.

Each test in this module fails on the pre-P0 code:

* ``F2`` subscribe() was sent from ``_start_ticker`` before ``on_connect``, so
  ``kiteconnect`` closed the socket and the feed never subscribed;
* ``F20`` ``stop()`` flipped ``_stopped`` and every later ``start()`` was a
  no-op, so the feed could never be rebuilt;
* ``F5`` there was no single availability answer for the surfaces to share;
* ``F19`` the forming candle was keyed off the wall clock, not the tick;
* ``F24`` the backoff constants were unused.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from fakes.fake_kite import FakeKite, FakeTicker
from kite_alerts.core.clock import IST
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
    SSEClient,
    _backoff_delay,
)


@pytest.fixture(autouse=True)
def _strict_socket() -> object:
    """Model the real SDK: connect() is async, subscribe() needs a socket."""
    FakeTicker.reset()
    FakeTicker.strict_socket = True
    yield
    FakeTicker.reset()


def _make_feed() -> LiveFeedService:
    fake_kite = FakeKite()
    return LiveFeedService(
        ticker_factory=lambda key, token: FakeTicker(key, token),
        kite_factory=lambda: fake_kite,
    )


class TestSubscribeOrdering:
    def test_subscribe_before_connect_is_queued_not_sent(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        feed.subscribe([("NSE:INFY", 1001, False)])

        ticker = FakeTicker.instances[-1]
        # Nothing was sent, and the socket was not closed by a bad subscribe.
        assert ticker.subscribed == []
        assert ticker.closed is False
        assert feed.status().subscribed == 0

    def test_queue_is_flushed_once_the_socket_opens(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        feed.subscribe([("NSE:INFY", 1001, False), ("NFO:NIFTY-FUT", 2001, True)])

        FakeTicker.instances[-1].open_socket()

        ticker = FakeTicker.instances[-1]
        assert sorted(ticker.subscribed) == [1001, 2001]
        assert ("quote", [1001]) in ticker.modes
        assert ("full", [2001]) in ticker.modes
        assert feed.status().subscribed == 2
        assert feed.status().state == STATE_LIVE

    def test_reconnect_resubscribes_accepted_tokens(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        feed.subscribe([("NSE:INFY", 1001, False)])
        ticker = FakeTicker.instances[-1]
        ticker.open_socket()
        assert ticker.subscribed == [1001]

        # The socket drops: both the counter and the exchange view must reset.
        ticker.drop()
        assert feed.status().subscribed == 0

        ticker.reconnect(1)
        ticker.open_socket()
        assert ticker.subscribed == [1001, 1001]


class TestPauseAndRestart:
    def test_pause_is_not_terminal(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        feed.subscribe([("NSE:INFY", 1001, False)])
        FakeTicker.instances[-1].open_socket()

        feed.pause()

        assert feed.status().state == STATE_DISCONNECTED
        assert feed.status().subscribed == 0
        assert FakeTicker.instances[-1].closed is True

        feed.start(api_key="k", access_token="t")
        assert len(FakeTicker.instances) == 2
        assert feed.status().state == STATE_CONNECTING

        FakeTicker.instances[-1].open_socket()
        assert FakeTicker.instances[-1].subscribed == [1001]

    def test_stop_is_terminal(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        feed.stop()
        feed.start(api_key="k", access_token="t")

        assert len(FakeTicker.instances) == 1
        assert feed.status().state == STATE_DISCONNECTED
        assert feed.admit_sse_client(SSEClient("NSE:INFY")) is False

    def test_late_callbacks_from_a_replaced_socket_are_ignored(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        old = FakeTicker.instances[-1]
        old.open_socket()

        feed.pause()
        feed.start(api_key="k", access_token="t")
        FakeTicker.instances[-1].open_socket()

        # The dead socket must not resurrect the feed or its subscription count.
        old.push([{"instrument_token": 1001, "last_price": 99.0}])
        old.drop()

        assert feed.status().state == STATE_LIVE
        assert feed.status().subscribed == 0


class TestAvailability:
    def test_connected_feed_is_enabled_and_says_so(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        FakeTicker.instances[-1].open_socket()

        availability = feed.availability()

        assert availability.enabled is True
        assert availability.available is True
        assert availability.state == STATE_LIVE
        assert "live" in availability.notice.lower()
        assert "c4" not in availability.notice.lower()

    def test_before_start_the_notice_does_not_promise_something_else(self) -> None:
        feed = _make_feed()
        availability = feed.availability()

        assert availability.state == STATE_DISCONNECTED
        assert availability.enabled is False
        for forbidden in ("c4", "later update", "coming soon", "not yet available"):
            assert forbidden not in availability.notice.lower()

    def test_token_expiry_disables_the_source(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        FakeTicker.instances[-1].open_socket()
        feed.set_token_expired()

        availability = feed.availability()

        assert availability.state == STATE_TOKEN_EXPIRED
        assert availability.enabled is False
        assert "reconnect" in availability.notice.lower()

    def test_simulated_data_is_never_labelled_live(self) -> None:
        feed = LiveFeedService(
            ticker_factory=lambda key, token: FakeTicker(key, token),
            kite_factory=lambda: FakeKite(),
            is_simulated=True,
        )
        feed.start(api_key="k", access_token="t")

        availability = feed.availability()

        assert availability.state == STATE_SIMULATED
        assert availability.badge == "SIMULATED"
        assert "simulated" in availability.notice.lower()

    @pytest.mark.parametrize(
        "state",
        [
            STATE_DISCONNECTED,
            STATE_CONNECTING,
            STATE_LIVE,
            STATE_STALE,
            STATE_MARKET_CLOSED,
            STATE_TOKEN_EXPIRED,
            STATE_NO_PLAN,
            STATE_SIMULATED,
        ],
    )
    def test_every_state_has_exactly_one_notice(self, state: str) -> None:
        from kite_alerts.ui.live_feed import _AVAILABILITY_TEXT

        assert _AVAILABILITY_TEXT[state]
        assert "_" not in _AVAILABILITY_TEXT[state]


class TestExchangeTimeCandles:
    def test_forming_candle_uses_the_tick_time_not_the_wall_clock(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        feed.subscribe([("NSE:INFY", 1001, False)])
        ticker = FakeTicker.instances[-1]
        ticker.open_socket()

        client = SSEClient("NSE:INFY")
        assert feed.admit_sse_client(client)

        # A tick that arrives late: exchange time is ten minutes in the past.
        tick_time = (datetime.now(tz=IST) - timedelta(minutes=10)).replace(second=5, microsecond=0)
        ticker.push(
            [
                {
                    "instrument_token": 1001,
                    "last_price": 1500.0,
                    "volume_traded": 10,
                    "exchange_timestamp": tick_time.replace(tzinfo=None),
                }
            ]
        )

        events = []
        while client.queue_size:
            event = client.get(0.0)
            assert event is not None
            events.append(event)

        forming = [event for event in events if event.event == "forming"]
        assert forming, "expected a forming candle for the tick's minute"
        candle = forming[-1].data
        assert candle["ist"] == tick_time.strftime("%Y-%m-%d %H:%M")


class TestSSEAdmission:
    def test_cap_is_enforced_under_one_lock(self) -> None:
        from kite_alerts.ui.live_feed import _MAX_SSE_CLIENTS

        feed = _make_feed()
        admitted = [feed.admit_sse_client(SSEClient()) for _ in range(_MAX_SSE_CLIENTS)]

        assert all(admitted)
        assert feed.admit_sse_client(SSEClient()) is False


class TestBackoff:
    def test_backoff_grows_then_caps(self) -> None:
        rng = random.Random(0)
        first = _backoff_delay(1, rng)
        second = _backoff_delay(2, rng)
        capped = _backoff_delay(50, rng)

        assert 1.0 <= first <= 1.25
        assert 2.0 <= second <= 2.5
        assert 60.0 <= capped <= 75.0
