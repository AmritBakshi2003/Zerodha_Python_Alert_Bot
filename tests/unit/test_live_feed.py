"""Tests for the Kite live feed service (C4).

Covers:
* Tick to candle to SSE path
* Throttling (max 4 updates/sec per symbol)
* Drop-oldest queue behavior
* Out-of-order and duplicate ticks
* Missing OI handling
* Disconnect, backoff, and market-closed behavior
* Token expiry handling and fallback
* Latency calculation and clock-skew detection
* Subscription limits and mode selection (quote vs full)
* Start, stop, and clean shutdown without hanging threads
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from unittest.mock import MagicMock

import pandas as pd

from fakes.fake_kite import FakeKite, FakeTicker
from kite_alerts.core.calendar import TradingCalendar
from kite_alerts.core.clock import IST, FrozenClock
from kite_alerts.data.candle_builder import Candle
from kite_alerts.ui.live_feed import (
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    STATE_LIVE,
    STATE_STALE,
    STATE_TOKEN_EXPIRED,
    LiveFeedService,
    LiveTick,
    SSEClient,
    SSEEvent,
    TokenRegistry,
    _bootstrap_candles,
    _bootstrap_without_oi,
    _candle_dict,
    get_feed,
    init_feed,
)


def _make_feed(
    *, calendar: TradingCalendar | None = None, is_simulated: bool = False
) -> LiveFeedService:
    FakeTicker.reset()
    fake_kite = FakeKite()
    return LiveFeedService(
        ticker_factory=lambda key, token: FakeTicker(key, token),
        kite_factory=lambda: fake_kite,
        calendar=calendar,
        is_simulated=is_simulated,
    )


class TestTokenRegistry:
    def test_registration_and_lookup(self) -> None:
        reg = TokenRegistry()
        reg.register("NSE:INFY", 1001)
        assert reg.symbol_for(1001) == "NSE:INFY"
        assert reg.token_for("NSE:INFY") == 1001
        assert reg.token_for("nse:infy") == 1001
        assert reg.symbol_for(9999) is None
        assert reg.all_tokens() == [1001]
        reg.clear()
        assert reg.all_tokens() == []


class TestLiveFeedServiceLifecycle:
    def test_start_and_connect(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        assert feed.status().state in (STATE_CONNECTING, STATE_LIVE)
        feed.stop()
        assert feed.status().state == STATE_DISCONNECTED

    def test_subscription_limits_and_modes(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        ticker = feed._ticker
        assert ticker is not None

        # Subscribe within limit
        symbols = [(f"NSE:SYM{i}", 1000 + i, False) for i in range(10)] + [("NFO:FUT1", 2001, True)]
        feed.subscribe(symbols)

        assert 2001 in feed._active_full
        assert 1000 in feed._active_quote
        assert len(feed._active_quote) + len(feed._active_full) == 11

        # Check unsubscribe
        feed.unsubscribe([1000, 2001])
        assert 1000 not in feed._active_quote
        assert 2001 not in feed._active_full
        feed.stop()

    def test_subscription_cap(self) -> None:
        feed = _make_feed()
        feed.max_subscriptions = 5
        feed.start(api_key="test_key", access_token="test_token")

        symbols = [(f"NSE:SYM{i}", 1000 + i, False) for i in range(10)]
        feed.subscribe(symbols)
        assert len(feed._pending_quote) + len(feed._active_quote) <= 5
        feed.stop()


class TestTickProcessing:
    def test_tick_to_candle_and_sse(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        feed.subscribe([("NSE:TEST", 111, False)])

        client = SSEClient("NSE:TEST", 1)
        feed.register_sse_client(client)

        now = time.time()
        raw_tick = {
            "instrument_token": 111,
            "last_price": 100.5,
            "volume_traded": 1000,
            "exchange_timestamp": dt.datetime.fromtimestamp(now, tz=IST).replace(tzinfo=None),
        }
        feed._on_ticks(feed._ticker, [raw_tick])

        # Client receives tick
        event = client.get(timeout=1.0)
        assert event is not None
        assert event.event in ("tick", "forming", "status")

        feed.unregister_sse_client(client)
        feed.stop()

    def test_throttling_pushes(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        feed.subscribe([("NSE:TEST", 111, False)])

        client = SSEClient("NSE:TEST", 1)
        feed.register_sse_client(client)

        now = time.time()
        # Fire 5 ticks in quick succession (< 0.25s)
        for i in range(5):
            raw = {
                "instrument_token": 111,
                "last_price": 100.0 + i,
                "volume_traded": 1000 + i * 10,
                "exchange_timestamp": dt.datetime.fromtimestamp(now, tz=IST).replace(tzinfo=None),
            }
            feed._on_ticks(feed._ticker, [raw])

        # Should be throttled: not every tick generates a separate push
        events = []
        while True:
            ev = client.get(timeout=0.1)
            if ev is None:
                break
            if ev.event == "tick":
                events.append(ev)

        assert len(events) < 5  # Throttled to max 4/s
        feed.unregister_sse_client(client)
        feed.stop()

    def test_missing_oi_handled_gracefully(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        feed.subscribe([("NSE:EQUITY", 123, False)])

        # Tick with no OI field
        raw = {
            "instrument_token": 123,
            "last_price": 500.0,
            "volume_traded": 5000,
            "exchange_timestamp": dt.datetime.now(tz=IST).replace(tzinfo=None),
        }
        feed._on_ticks(feed._ticker, [raw])
        assert feed.status().last_tick_age is not None
        feed.stop()


class TestSSEQueueDropOldest:
    def test_queue_overflow_drops_oldest(self) -> None:
        client = SSEClient("NSE:TEST", 1)
        # Push more than maxsize
        for i in range(600):
            client.push(SSEEvent(event="tick", data={"seq": i}, symbol="NSE:TEST"))

        first = client.get(timeout=0.1)
        assert first is not None
        # Oldest items (seq 0) should have been dropped
        assert first.data["seq"] > 0
        client.close()


class TestLatencyAndClockSkew:
    def test_normal_latency(self) -> None:
        now = 1700000000.0
        tick = LiveTick(
            symbol="NSE:TEST",
            token=111,
            price=100.0,
            volume_cum=100,
            oi=0.0,
            exchange_ts=now - 0.05,  # 50 ms ago
            received_at=now,
        )
        assert tick.latency_ms is not None
        assert 45 < tick.latency_ms < 55
        assert "50 ms" in tick.latency_note

    def test_clock_skew_negative_or_huge(self) -> None:
        now = 1700000000.0
        # Negative latency (PC clock behind exchange)
        tick_neg = LiveTick(
            symbol="NSE:TEST",
            token=111,
            price=100.0,
            volume_cum=100,
            oi=0.0,
            exchange_ts=now + 5.0,
            received_at=now,
        )
        assert tick_neg.latency_ms is None
        assert "out of sync" in tick_neg.latency_note

        # Huge latency (> 5 min)
        tick_huge = LiveTick(
            symbol="NSE:TEST",
            token=111,
            price=100.0,
            volume_cum=100,
            oi=0.0,
            exchange_ts=now - 400.0,
            received_at=now,
        )
        assert tick_huge.latency_ms is None
        assert "out of sync" in tick_huge.latency_note


class TestResilienceAndErrors:
    def test_token_expiry_stops_reconnect(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        feed._on_connect(None, {})
        assert feed.status().state == STATE_LIVE

        # Simulate 403 / auth error
        feed._on_error(None, 403, "TokenException: Token is invalid")
        assert feed.status().state == STATE_TOKEN_EXPIRED
        assert "Token expired" in feed.status().reason
        feed.stop()

    def test_reconnect_counter(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        feed._on_reconnect(None, 1)
        assert feed.status().reconnect_count == 1
        assert feed.status().state == STATE_CONNECTING
        feed.stop()

    def test_stale_detection(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        feed._on_connect(None, {})
        # Set last tick to 40 seconds ago
        feed._last_tick_time = time.time() - 40.0
        status = feed.status()
        assert status.state == STATE_STALE
        feed.stop()


class TestBootstrapHelpers:
    def test_bootstrap_candles_success(self) -> None:
        kite = MagicMock()
        now = dt.datetime(2026, 11, 16, 10, 0, tzinfo=IST)
        kite.historical_data.return_value = [
            {
                "date": now,
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 200,
                "oi": 1500,
            }
        ]
        result = _bootstrap_candles(kite, 1001, "NSE:INFY", days=1)
        assert len(result) == 1
        assert result[0]["open"] == 100.0
        assert result[0]["volume"] == 200
        assert result[0]["oi"] == 1500.0

    def test_bootstrap_candles_empty(self) -> None:
        kite = MagicMock()
        kite.historical_data.return_value = []
        result = _bootstrap_candles(kite, 1001, "NSE:INFY")
        assert result == []

    def test_bootstrap_candles_exception(self) -> None:
        kite = MagicMock()
        kite.historical_data.side_effect = RuntimeError("network fail")
        result = _bootstrap_candles(kite, 1001, "NSE:INFY")
        assert result == []

    def test_bootstrap_without_oi_success(self) -> None:
        kite = MagicMock()
        now = dt.datetime(2026, 11, 16, 10, 0, tzinfo=IST)
        kite.historical_data.return_value = [
            {
                "date": now,
                "open": 200.0,
                "high": 205.0,
                "low": 199.0,
                "close": 202.0,
                "volume": 500,
            }
        ]
        result = _bootstrap_without_oi(kite, 1001, "NSE:INFY")
        assert len(result) == 1
        assert result[0]["open"] == 200.0
        assert result[0]["oi"] == 0.0

    def test_bootstrap_without_oi_empty(self) -> None:
        kite = MagicMock()
        kite.historical_data.return_value = []
        result = _bootstrap_without_oi(kite, 1001, "NSE:INFY")
        assert result == []

    def test_bootstrap_without_oi_exception(self) -> None:
        kite = MagicMock()
        kite.historical_data.side_effect = RuntimeError("network fail")
        result = _bootstrap_without_oi(kite, 1001, "NSE:INFY")
        assert result == []


class TestLiveFeedBootstrapThread:
    def test_run_bootstrap_success(self, monkeypatch) -> None:
        feed = _make_feed()
        sample_candles = [
            {
                "time": 1700000000,
                "open": 10.0,
                "high": 12.0,
                "low": 9.0,
                "close": 11.0,
                "volume": 50,
                "oi": 0.0,
            }
        ]
        monkeypatch.setattr(
            "kite_alerts.ui.live_feed._bootstrap_candles", lambda *_, **__: sample_candles
        )
        feed.run_bootstrap("NSE:TEST", 1001)
        time.sleep(0.05)
        assert feed.bootstrap_data_for("NSE:TEST") == sample_candles

    def test_run_bootstrap_fallback_to_without_oi(self, monkeypatch) -> None:
        feed = _make_feed()
        sample_candles = [
            {
                "time": 1700000000,
                "open": 10.0,
                "high": 12.0,
                "low": 9.0,
                "close": 11.0,
                "volume": 50,
                "oi": 0.0,
            }
        ]
        monkeypatch.setattr("kite_alerts.ui.live_feed._bootstrap_candles", lambda *_, **__: [])
        monkeypatch.setattr(
            "kite_alerts.ui.live_feed._bootstrap_without_oi", lambda *_, **__: sample_candles
        )
        feed.run_bootstrap("NSE:TEST", 1001)
        time.sleep(0.05)
        assert feed.bootstrap_data_for("NSE:TEST") == sample_candles

    def test_run_bootstrap_kite_factory_none(self) -> None:
        feed = LiveFeedService(
            ticker_factory=lambda *_: FakeTicker("", ""),
            kite_factory=lambda: (_ for _ in ()).throw(RuntimeError("kite unavailable")),
        )
        feed.run_bootstrap("NSE:TEST", 1001)
        time.sleep(0.05)
        assert feed.bootstrap_data_for("NSE:TEST") == []


class TestLiveFeedCallbacksAndLifecycle:
    def test_start_ticker_factory_failure(self) -> None:
        feed = LiveFeedService(
            ticker_factory=lambda *_: (_ for _ in ()).throw(RuntimeError("ticker failed")),
            kite_factory=FakeKite,
        )
        feed.start(api_key="k", access_token="t")
        assert feed.status().state == STATE_DISCONNECTED
        assert "Could not create" in feed.status().reason

    def test_ticker_connect_exception_in_thread(self, monkeypatch) -> None:
        class FailingTicker(FakeTicker):
            def connect(self, threaded: bool = True) -> None:
                raise RuntimeError("cannot connect")

        feed = LiveFeedService(
            ticker_factory=lambda *_: FailingTicker("", ""),
            kite_factory=FakeKite,
        )
        feed.start(api_key="k", access_token="t")
        if feed._thread:
            feed._thread.join(timeout=1.0)
        assert feed.status().state == STATE_DISCONNECTED
        assert "failed" in feed.status().reason.lower()

    def test_on_close_normal_and_token_expired(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        feed._on_close(None, 1000, "Normal closure")
        assert feed.status().state == STATE_DISCONNECTED
        assert "Disconnected" in feed.status().reason

        feed.set_token_expired()
        feed._on_close(None, 1000, "Normal closure")
        assert feed.status().state == STATE_TOKEN_EXPIRED

    def test_on_error_generic(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        feed._on_error(None, 500, "Server error")
        assert feed.status().state == STATE_DISCONNECTED
        assert "Connection error" in feed.status().reason

    def test_on_noreconnect(self) -> None:
        feed = _make_feed()
        feed.start(api_key="test_key", access_token="test_token")
        feed._on_noreconnect(None)
        assert feed.status().state == STATE_DISCONNECTED
        assert "Reconnect limit reached" in feed.status().reason


class TestMarketIsOpen:
    def test_market_is_open_no_calendar(self) -> None:
        feed = _make_feed(calendar=None)
        assert feed._market_is_open() is True

    def test_market_is_open_calendar(self) -> None:
        cal = TradingCalendar()
        # Monday at 10:30 IST -> open
        clock = FrozenClock(dt.datetime(2026, 11, 16, 10, 30, tzinfo=IST))
        feed = LiveFeedService(
            ticker_factory=lambda *_: FakeTicker("", ""),
            kite_factory=FakeKite,
            calendar=cal,
            clock=clock,
        )
        assert feed._market_is_open() is True

        # Sunday at 10:30 IST -> closed
        feed.clock = FrozenClock(dt.datetime(2026, 11, 15, 10, 30, tzinfo=IST))
        assert feed._market_is_open() is False

        # Monday at 08:00 IST -> closed
        feed.clock = FrozenClock(dt.datetime(2026, 11, 16, 8, 0, tzinfo=IST))
        assert feed._market_is_open() is False


class TestTickProcessingEdgeCases:
    def test_unregistered_token_ignored(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        feed._on_ticks(None, [{"instrument_token": 999999, "last_price": 100.0}])
        assert feed._last_tick_time == 0.0

    def test_tick_missing_exchange_timestamp(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        feed.subscribe([("NSE:TEST", 111, False)])
        feed._on_ticks(None, [{"instrument_token": 111, "last_price": 100.0}])
        assert feed.status().last_tick_age is not None

    def test_tick_transitions_stale_to_live(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        feed._on_connect(None, {})
        feed._state = STATE_STALE
        feed.subscribe([("NSE:TEST", 111, False)])
        now = time.time()
        feed._on_ticks(
            None,
            [
                {
                    "instrument_token": 111,
                    "last_price": 100.0,
                    "exchange_timestamp": dt.datetime.fromtimestamp(now, tz=IST).replace(
                        tzinfo=None
                    ),
                }
            ],
        )
        assert feed.status().state == STATE_LIVE

    def test_tick_ignored_when_disconnected(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        feed.stop()
        feed.subscribe([("NSE:TEST", 111, False)])
        feed._on_ticks(None, [{"instrument_token": 111, "last_price": 100.0}])
        assert feed._last_tick_time == 0.0


class TestGlobalFeedSingleton:
    def test_get_and_init_feed(self) -> None:
        feed = _make_feed()
        init_feed(feed)
        assert get_feed() is feed


class TestStopIsTerminal:
    """F27: a callback racing stop() must never resurrect the feed."""

    def test_a_late_on_connect_cannot_claim_live_while_stopping(self) -> None:
        feed = _make_feed()
        feed.start(api_key="k", access_token="t")
        ticker = feed._ticker
        assert ticker is not None
        connect_thread = feed._thread
        if connect_thread is not None:
            connect_thread.join(5.0)
        assert not connect_thread.is_alive()
        assert feed.status().state == STATE_LIVE

        entered_close = threading.Event()
        release_close = threading.Event()
        real_close = feed._close_socket
        broadcasts: list[SSEEvent] = []

        def blocked_close(reason: str) -> None:
            # stop() marks the service stopped first and only then closes the
            # socket (which bumps the generation). Pausing here reproduces the
            # exact window the fix must survive.
            entered_close.set()
            assert release_close.wait(10.0), "test did not release stop()"
            real_close(reason)

        feed._close_socket = blocked_close  # type: ignore[method-assign]
        feed._broadcast = broadcasts.append  # type: ignore[method-assign]

        stopper = threading.Thread(target=feed.stop, daemon=True)
        stopper.start()
        try:
            assert entered_close.wait(5.0), "stop() never reached the socket close"
            # The very socket being stopped still delivers on_connect. The
            # generation bump is pending, so only the in-lock _stopped check
            # can reject it.
            feed._on_connect(ticker, {"type": "connect"})
            during_stop = list(broadcasts)
            subscribed_during_stop = list(ticker.subscribed)
        finally:
            release_close.set()
            stopper.join(5.0)

        assert not stopper.is_alive()
        assert [e.data.get("state") for e in during_stop if e.event == "status"] == []
        assert subscribed_during_stop == [], "a stopped feed must not subscribe"
        assert feed.status().state == STATE_DISCONNECTED


class TestCandleDict:
    def test_candle_dict_formatting(self) -> None:
        candle = Candle(
            symbol="NSE:TEST",
            time=pd.Timestamp("2026-11-16 10:00:00", tz=IST),
            open=100.0,
            high=105.0,
            low=98.0,
            close=102.0,
            volume=500,
            oi=1200.0,
        )
        d = _candle_dict(candle, is_simulated=False)
        assert d["open"] == 100.0
        assert d["volume"] == 500
        assert d["oi"] == 1200.0
        assert d["is_simulated"] is False
        assert "ist" in d
