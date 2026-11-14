"""Websocket adapter: tick mapping, stale timestamps, auth detection, stop()."""

from __future__ import annotations

import pandas as pd

from fakes.fake_kite import FakeTicker
from kite_alerts.config.settings import ResilienceSettings
from kite_alerts.core.clock import FrozenClock
from kite_alerts.core.models import Tick
from kite_alerts.data.feed import KiteFeed
from kite_alerts.observability.metrics import Metrics


class Sink:
    def __init__(self, *, boom: bool = False) -> None:
        self.ticks: list[Tick] = []
        self.boom = boom

    def __call__(self, tick: Tick) -> None:
        if self.boom:
            raise RuntimeError("downstream exploded")
        self.ticks.append(tick)


def build_feed(
    *, sink: Sink | None = None, factory=None, **kwargs
) -> tuple[KiteFeed, Sink, Metrics]:
    FakeTicker.reset()
    sink = sink or Sink()
    metrics = Metrics()
    feed = KiteFeed(
        api_key="key",
        access_token="token",
        token_map={"NSE:TEST": 111},
        sink=sink,
        settings=ResilienceSettings(),
        metrics=metrics,
        clock=FrozenClock("2026-11-16 10:00:00"),
        ticker_factory=factory or FakeTicker,
        **kwargs,
    )
    return feed, sink, metrics


def tick(**overrides):
    base = {
        "instrument_token": 111,
        "last_price": 100.5,
        "exchange_timestamp": pd.Timestamp("2026-11-16 10:00:01"),
        "volume_traded": 1234,
        "oi": 5000.0,
    }
    base.update(overrides)
    return base


class TestConnection:
    def test_start_subscribes_in_full_mode(self) -> None:
        feed, _, _ = build_feed()
        feed.start()
        ticker = FakeTicker.instances[-1]
        assert ticker.subscribed == [111]
        assert ticker.modes == [(FakeTicker.MODE_FULL, [111])]
        assert ticker.connect_args == (True,)
        assert feed.connected is True

    def test_large_watchlists_are_subscribed_in_batches(self) -> None:
        FakeTicker.reset()
        tokens = {f"NSE:S{i}": i for i in range(1, 226)}
        feed = KiteFeed(
            api_key="k",
            access_token="t",
            token_map=tokens,
            sink=Sink(),
            settings=ResilienceSettings(),
            ticker_factory=FakeTicker,
        )
        feed.start()
        ticker = FakeTicker.instances[-1]
        assert len(ticker.subscribed) == 225
        assert len(ticker.modes) == 2  # 200 + 25

    def test_stop_is_idempotent_and_exception_safe(self) -> None:
        feed, _, _ = build_feed()
        feed.start()
        ticker = FakeTicker.instances[-1]
        feed.stop()
        feed.stop()
        assert ticker.stopped
        assert ticker.closed
        assert feed.connected is False

    def test_stop_without_start(self) -> None:
        feed, _, _ = build_feed()
        feed.stop()  # must not raise

    def test_ticker_without_optional_callbacks(self) -> None:
        class MinimalTicker:
            MODE_FULL = "full"

            def __init__(self, *args, **kwargs) -> None:
                self.subscribed = []
                self.modes = []

            def connect(self, threaded: bool = False, **_) -> None:
                return None

            def subscribe(self, tokens) -> None:
                self.subscribed.extend(tokens)

            def set_mode(self, mode, tokens) -> None:
                self.modes.append(mode)

            def stop(self) -> None:
                return None

            def close(self) -> None:
                return None

        feed, _, _ = build_feed(factory=MinimalTicker)
        feed.start()  # must not fail on the missing on_reconnect attribute
        feed.stop()

    def test_connect_hook_reports_first_and_later_connections(self) -> None:
        flags: list[bool] = []
        feed, _, _ = build_feed(on_connect_hook=flags.append)
        feed.start()
        FakeTicker.instances[-1].connect(threaded=True)
        assert flags == [True, False]


class TestTickMapping:
    def test_tick_is_translated(self) -> None:
        feed, sink, metrics = build_feed()
        feed.start()
        FakeTicker.instances[-1].push([tick()])
        assert len(sink.ticks) == 1
        translated = sink.ticks[0]
        assert translated.symbol == "NSE:TEST"
        assert translated.price == 100.5
        assert translated.volume_cum == 1234
        assert translated.oi == 5000.0
        assert metrics.get("ticks_received") == 1

    def test_unknown_token_is_ignored(self) -> None:
        feed, sink, _ = build_feed()
        feed.start()
        FakeTicker.instances[-1].push([tick(instrument_token=999)])
        assert sink.ticks == []

    def test_missing_price_is_ignored(self) -> None:
        feed, sink, metrics = build_feed()
        feed.start()
        raw = tick()
        del raw["last_price"]
        FakeTicker.instances[-1].push([raw])
        assert sink.ticks == []
        assert metrics.get("invalid_ticks") == 0

    def test_non_positive_price_is_counted(self) -> None:
        feed, sink, metrics = build_feed()
        feed.start()
        FakeTicker.instances[-1].push([tick(last_price=0.0)])
        assert sink.ticks == []
        assert metrics.get("invalid_ticks") == 1

    def test_stale_last_trade_time_is_replaced_with_now(self) -> None:
        feed, sink, metrics = build_feed()
        feed.start()
        raw = tick()
        del raw["exchange_timestamp"]
        raw["last_trade_time"] = pd.Timestamp("2026-11-16 08:30:00")  # 90 minutes old
        FakeTicker.instances[-1].push([raw])
        # A stale timestamp is replaced with the clock so the candle lands in the
        # right minute instead of being replayed into a long-closed one.
        assert sink.ticks[0].ts.strftime("%H:%M") == "10:00"
        assert metrics.get("stale_tick_timestamps") == 1

    def test_missing_timestamps_use_the_clock(self) -> None:
        feed, sink, _ = build_feed()
        feed.start()
        raw = tick()
        del raw["exchange_timestamp"]
        FakeTicker.instances[-1].push([raw])
        assert sink.ticks[0].ts.strftime("%H:%M") == "10:00"

    def test_sink_exception_does_not_kill_the_socket_thread(self) -> None:
        sink = Sink(boom=True)
        feed, _, metrics = build_feed(sink=sink)
        feed.start()
        FakeTicker.instances[-1].push([tick(), tick()])
        assert metrics.get("tick_errors") == 2

    def test_malformed_tick_does_not_raise(self) -> None:
        feed, _, metrics = build_feed()
        feed.start()
        FakeTicker.instances[-1].push([{"instrument_token": "not-an-int"}])
        assert metrics.get("tick_errors") == 1


class TestAuthDetection:
    def test_close_with_403_reports_an_auth_error(self) -> None:
        messages: list[str] = []
        feed, _, metrics = build_feed(on_auth_error=messages.append)
        feed.start()
        FakeTicker.instances[-1].drop(403, "Forbidden")
        assert messages
        assert "login" in messages[0]
        assert metrics.get("auth_errors") == 1

    def test_error_with_token_text_reports_an_auth_error(self) -> None:
        messages: list[str] = []
        feed, _, _ = build_feed(on_auth_error=messages.append)
        feed.start()
        FakeTicker.instances[-1].error(0, "TokenException: invalid token")
        assert messages

    def test_ordinary_disconnect_does_not_report_auth(self) -> None:
        messages: list[str] = []
        feed, _, metrics = build_feed(on_auth_error=messages.append)
        feed.start()
        FakeTicker.instances[-1].drop(1006, "connection closed")
        assert messages == []
        assert metrics.get("ws_closes") == 1

    def test_reconnect_callbacks_are_counted(self) -> None:
        feed, _, metrics = build_feed()
        feed.start()
        ticker = FakeTicker.instances[-1]
        ticker.reconnect(3)
        assert metrics.get("ws_reconnects") == 1

    def test_token_map_is_exposed_read_only(self) -> None:
        feed, _, _ = build_feed()
        assert feed.token_map == {111: "NSE:TEST"}
