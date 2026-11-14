"""The full live pipeline, offline: seed -> ticks -> candles -> signal -> notify.

This is the path that had never been exercised before (it required a real Kite
session). It replays the same synthetic day the demo uses, but through the real
feed, queue, clock-driven candle builder, engine, dedupe store and notifier hub.
"""

from __future__ import annotations

import pandas as pd
import pytest

from e2e.support import Harness, build_harness, ticks_for_bar

pytestmark = pytest.mark.e2e


def replay_session(harness: Harness) -> int:
    """Push the session through the live path; returns the number of alerts."""
    ticker = harness.start_feed()
    harness.step()  # process the first-connect back-fill command

    volume = 0
    for index, (ts, row) in enumerate(harness.session.iterrows()):
        volume += 1000
        harness.clock.set(ts + pd.Timedelta(seconds=1))
        ticker.push(ticks_for_bar(ts, row, cumulative_volume=volume))
        harness.step(4)  # one tick per step
        harness.clock.set(ts + pd.Timedelta(seconds=65))
        harness.step()  # clock closes the candle
        del index
    harness.runtime.hub.flush(timeout=10)
    return len([m for m in harness.notifier.messages if m.level == "alert"])


def test_pipeline_produces_the_same_alerts_as_the_engine_demo(tmp_path) -> None:
    harness = build_harness(tmp_path)
    try:
        count = replay_session(harness)
        alerts = [m for m in harness.notifier.messages if m.level == "alert"]

        assert count == 3
        assert [m.text.split()[1] for m in alerts] == ["SELL", "SELL", "BUY"]
        assert [
            "[09:34]" in alerts[0].text,
            "[09:49]" in alerts[1].text,
            "[10:07]" in alerts[2].text,
        ] == [
            True,
            True,
            True,
        ]
        # Same text the demo prints, including the reasons.
        assert alerts[0].text == (
            "🔴 SELL NSE:TEST @ 99.83 [09:34] — RSI 40.9 in bearish zone; "
            "MACD below signal, histogram falling; ORB breakdown below 99.89; "
            "OI +0.67% with price down (short buildup)"
        )
        assert "EMA9 crossed above EMA21" in alerts[2].text
    finally:
        harness.runtime.shutdown()


def test_feed_subscribes_in_full_mode(tmp_path) -> None:
    harness = build_harness(tmp_path)
    try:
        ticker = harness.start_feed()
        assert ticker.connected is True
        assert ticker.subscribed == [111]
        assert ticker.modes == [(ticker.MODE_FULL, [111])]
        assert ticker.connect_args == (True,)  # threaded, so the worker owns the loop
    finally:
        harness.runtime.shutdown()


def test_seeding_and_metrics(tmp_path) -> None:
    harness = build_harness(tmp_path)
    try:
        # build_runtime seeded the warm-up history before any tick arrived.
        assert harness.runtime.engine.snapshot()["NSE:TEST"]["bars"] == 50
        replay_session(harness)

        bars = len(harness.session)
        metrics = harness.runtime.metrics
        assert metrics.get("ticks_received") == bars * 4
        assert metrics.get("candles_closed", symbol="NSE:TEST") == bars
        assert metrics.get("alerts_fired", symbol="NSE:TEST") == 3
        assert metrics.get("alerts_sent") == 3
        assert harness.runtime.store.count_alerts() == 3
        assert harness.runtime.worker.queue_depth == 0
    finally:
        harness.runtime.shutdown()


def test_candle_volume_comes_from_cumulative_volume(tmp_path) -> None:
    harness = build_harness(tmp_path)
    try:
        ticker = harness.start_feed()
        harness.step()
        ts = harness.session.index[0]
        row = harness.session.iloc[0]
        harness.clock.set(ts + pd.Timedelta(seconds=1))
        ticker.push(ticks_for_bar(ts, row, cumulative_volume=10_000))
        harness.step(4)
        harness.clock.set(ts + pd.Timedelta(seconds=65))
        harness.step()

        frame = harness.runtime.engine.frame("NSE:TEST")
        assert frame is not None
        # The first tick of the minute sets the baseline, so the candle volume is
        # (last cumulative - first cumulative) = 1250 - 250.
        assert frame["volume"].iloc[-1] == 750
    finally:
        harness.runtime.shutdown()


def test_ticks_outside_the_session_are_ignored(tmp_path) -> None:
    harness = build_harness(tmp_path)
    try:
        ticker = harness.start_feed()
        harness.step()
        after_close = pd.Timestamp("2026-11-16 16:30", tz="Asia/Kolkata")
        harness.clock.set(after_close)
        ticker.push(
            [
                {
                    "instrument_token": 111,
                    "last_price": 100.0,
                    "exchange_timestamp": after_close.tz_localize(None).to_pydatetime(),
                    "volume_traded": 1,
                }
            ]
        )
        harness.step()
        assert harness.runtime.metrics.get("ticks_out_of_session", symbol="NSE:TEST") == 1
        assert harness.runtime.store.count_alerts() == 0
    finally:
        harness.runtime.shutdown()


def test_restart_does_not_resend(tmp_path) -> None:
    harness = build_harness(tmp_path)
    try:
        replay_session(harness)
        assert harness.runtime.store.count_alerts() == 3
        db_path = harness.runtime.settings.db_path
    finally:
        harness.runtime.shutdown()

    # Same database, fresh process-like engine: nothing pending is re-alerted.
    from kite_alerts.core.calendar import calendar_from_config
    from kite_alerts.engine.engine import Engine
    from kite_alerts.persistence.store import SqliteAlertStore

    store = SqliteAlertStore(db_path)
    try:
        engine = Engine(
            params_for=lambda _s: harness.runtime.settings.params,
            store=store,
            sink=_NullSink(),
            calendar=calendar_from_config(
                open_value="09:15", close_value="15:30", holiday_file=None
            ),
        )
        last_ts = harness.session.index[-1]
        assert store.last_candle("NSE:TEST") == last_ts
        from kite_alerts.core.models import Candle

        row = harness.session.iloc[-1]
        alert = engine.on_candle(
            Candle(
                symbol="NSE:TEST",
                time=last_ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=1000,
                oi=float(row["oi"]),
            )
        )
        assert alert is None
    finally:
        store.close()


class _NullSink:
    def submit(self, alert) -> None:
        return None
