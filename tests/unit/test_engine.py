"""Engine: idempotent candles, cooldown, restart safety, session guard."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest import make_frame
from fakes.stub_rules import all_bearish, all_bullish
from kite_alerts.config.settings import StrategyParams
from kite_alerts.core.calendar import TradingCalendar
from kite_alerts.core.models import Alert, Candle, Side
from kite_alerts.engine.engine import Engine
from kite_alerts.persistence.store import InMemoryAlertStore


class RecordingSink:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def submit(self, alert: Alert) -> None:
        self.alerts.append(alert)


def candles(frame: pd.DataFrame, symbol: str = "NSE:TEST") -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            time=ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
            oi=float(row["oi"]),
        )
        for ts, row in frame.iterrows()
    ]


def build_engine(
    *,
    store: InMemoryAlertStore | None = None,
    sink: RecordingSink | None = None,
    registry=None,
    params: StrategyParams | None = None,
    calendar: TradingCalendar | None = None,
) -> Engine:
    resolved_params = params or StrategyParams()
    return Engine(
        params_for=lambda _symbol: resolved_params,
        store=store or InMemoryAlertStore(),
        sink=sink or RecordingSink(),
        calendar=calendar or TradingCalendar(),
        registry=registry or all_bullish(),
    )


@pytest.fixture
def rising_frame() -> pd.DataFrame:
    """Exactly ``warmup_bars`` candles, so the final candle is the first evaluable one."""
    return make_frame(np.linspace(100, 104, 40))


class TestAlerting:
    def test_alert_fires_on_agreement(self, rising_frame: pd.DataFrame) -> None:
        sink = RecordingSink()
        engine = build_engine(sink=sink)
        for candle in candles(rising_frame):
            engine.on_candle(candle)
        assert len(sink.alerts) == 1
        assert sink.alerts[0].side is Side.BUY
        assert sink.alerts[0].rules_available == 5

    def test_no_alert_during_warmup(self) -> None:
        sink = RecordingSink()
        engine = build_engine(sink=sink)
        for candle in candles(make_frame(np.linspace(100, 101, 10))):
            engine.on_candle(candle)
        assert sink.alerts == []

    def test_duplicate_candle_is_ignored(self, rising_frame: pd.DataFrame) -> None:
        sink = RecordingSink()
        engine = build_engine(sink=sink)
        for candle in candles(rising_frame):
            engine.on_candle(candle)
        fired = len(sink.alerts)
        engine.on_candle(candles(rising_frame)[-1])
        assert len(sink.alerts) == fired

    def test_sink_failure_does_not_raise(self, rising_frame: pd.DataFrame) -> None:
        class BoomSink:
            def submit(self, alert: Alert) -> None:
                raise RuntimeError("downstream down")

        engine = build_engine(sink=BoomSink())
        alerts = [engine.on_candle(candle) for candle in candles(rising_frame)]
        assert any(alert is not None for alert in alerts)  # signal still produced


class TestCooldown:
    def test_same_side_within_cooldown_is_suppressed(self) -> None:
        params = StrategyParams(cooldown_minutes=15)
        frame = make_frame(np.linspace(100, 104, 41))  # one extra candle, 1 min later
        sink = RecordingSink()
        engine = build_engine(sink=sink, params=params)
        for candle in candles(frame):
            engine.on_candle(candle)
        assert len(sink.alerts) == 1

    def test_same_side_after_cooldown_fires_again(self) -> None:
        params = StrategyParams(cooldown_minutes=15)
        sink = RecordingSink()
        engine = build_engine(sink=sink, params=params)
        for candle in candles(make_frame(np.linspace(100, 104, 60))):
            engine.on_candle(candle)
        assert len(sink.alerts) == 2
        gap = sink.alerts[1].ts - sink.alerts[0].ts
        assert gap >= pd.Timedelta(minutes=15)

    def test_side_flip_is_allowed_by_default(self) -> None:
        frame = make_frame(np.linspace(100, 104, 40))
        sink = RecordingSink()
        store = InMemoryAlertStore()
        engine = build_engine(sink=sink, store=store)
        for candle in candles(frame):
            engine.on_candle(candle)
        assert sink.alerts
        assert sink.alerts[0].side is Side.BUY

        engine_bearish = Engine(
            params_for=lambda _symbol: StrategyParams(),
            store=store,
            sink=sink,
            calendar=TradingCalendar(),
            registry=all_bearish(),
        )
        engine_bearish.seed("NSE:TEST", frame)
        flip = Candle(
            symbol="NSE:TEST",
            time=sink.alerts[0].ts + pd.Timedelta(minutes=1),
            open=104.0,
            high=104.5,
            low=103.5,
            close=103.8,
            volume=100,
            oi=1e6,
        )
        alert = engine_bearish.on_candle(flip)
        assert alert is not None
        assert alert.side is Side.SELL

    def test_min_side_flip_minutes_damps_flips(self) -> None:
        params = StrategyParams(min_side_flip_minutes=30)
        frame = make_frame(np.linspace(100, 104, 40))
        store = InMemoryAlertStore()
        sink = RecordingSink()
        engine = build_engine(sink=sink, store=store, params=params)
        for candle in candles(frame):
            engine.on_candle(candle)
        assert sink.alerts
        bearish = Engine(
            params_for=lambda _symbol: params,
            store=store,
            sink=sink,
            calendar=TradingCalendar(),
            registry=all_bearish(),
        )
        bearish.seed("NSE:TEST", frame)
        flip = Candle(
            symbol="NSE:TEST",
            time=sink.alerts[0].ts + pd.Timedelta(minutes=5),
            open=104.0,
            high=104.5,
            low=103.5,
            close=103.8,
            volume=100,
            oi=1e6,
        )
        assert bearish.on_candle(flip) is None


class TestRestartSafety:
    def test_last_processed_candle_is_restored(self, sqlite_store) -> None:
        frame = make_frame(np.linspace(100, 104, 50))
        first_sink = RecordingSink()
        first = Engine(
            params_for=lambda _s: StrategyParams(),
            store=sqlite_store,
            sink=first_sink,
            calendar=TradingCalendar(),
            registry=all_bullish(),
        )
        for candle in candles(frame):
            first.on_candle(candle)
        assert len(first_sink.alerts) == 1

        # Simulate a restart: new engine, same database.
        second_sink = RecordingSink()
        second = Engine(
            params_for=lambda _s: StrategyParams(),
            store=sqlite_store,
            sink=second_sink,
            calendar=TradingCalendar(),
            registry=all_bullish(),
        )
        second.seed("NSE:TEST", frame.iloc[:-1])
        alert = second.on_candle(candles(frame)[-1])
        assert alert is None
        assert second_sink.alerts == []


class TestSessionGuard:
    def test_post_close_candle_is_ignored(self) -> None:
        sink = RecordingSink()
        engine = build_engine(sink=sink)
        after_close = Candle(
            symbol="NSE:TEST",
            time=pd.Timestamp("2026-11-16 16:05", tz="Asia/Kolkata"),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10,
            oi=1e6,
        )
        assert engine.on_candle(after_close) is None
        assert sink.alerts == []

    def test_holiday_candle_is_ignored(self, tmp_path) -> None:
        from kite_alerts.core.calendar import load_holidays

        holiday_file = tmp_path / "holidays.yaml"
        holiday_file.write_text("holidays:\n  - 2026-11-16\n", encoding="utf-8")
        calendar = TradingCalendar(holidays=load_holidays(holiday_file))
        engine = build_engine(calendar=calendar)
        for candle in candles(make_frame(np.linspace(100, 104, 50))):
            assert engine.on_candle(candle) is None


def test_snapshot_reports_buffers() -> None:
    engine = build_engine()
    engine.seed("NSE:TEST", make_frame(np.linspace(100, 101, 45)))
    snapshot = engine.snapshot()
    assert snapshot["NSE:TEST"]["bars"] == 45
    assert snapshot["NSE:TEST"]["last_close"] == pytest.approx(101.0)
