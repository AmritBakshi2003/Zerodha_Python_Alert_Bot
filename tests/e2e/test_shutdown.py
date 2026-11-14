"""Graceful shutdown: flush notifications, close the socket, release the database."""

from __future__ import annotations

import pandas as pd
import pytest

from .support import build_harness

pytestmark = pytest.mark.e2e


def test_shutdown_flushes_and_closes_everything(tmp_path) -> None:
    harness = build_harness(tmp_path)
    harness.runtime.start()
    try:
        harness.runtime.hub.flush(timeout=5)
        assert harness.runtime.feed.connected is True

        harness.runtime.shutdown(reason="test")
        harness.runtime.hub.flush(timeout=5)

        assert harness.notifier.closed is True
        assert harness.runtime.worker.stop_event.is_set() is True
        assert harness.ticker.stopped is True
        assert any("started" in text for text in harness.notifier.texts)
        assert any("stopping (test)" in text for text in harness.notifier.texts)

        # The store is closed: further writes fail loudly instead of silently.
        with pytest.raises(Exception):
            harness.runtime.store.count_alerts()
    finally:
        if harness.runtime.started:
            harness.runtime.shutdown()


def test_shutdown_is_idempotent(tmp_path) -> None:
    harness = build_harness(tmp_path)
    harness.runtime.start()
    harness.runtime.shutdown()
    harness.runtime.shutdown()  # must not raise


def test_engine_continues_after_a_notification_failure(tmp_path) -> None:
    """A dead channel must never stop candles being processed."""
    harness = build_harness(tmp_path)
    try:
        harness.notifier.fail_times = 99
        ticker = harness.start_feed()
        harness.step()
        ts = harness.session.index[0]
        row = harness.session.iloc[0]
        harness.clock.set(ts + pd.Timedelta(seconds=1))
        ticker.push(
            [
                {
                    "instrument_token": 111,
                    "last_price": float(row["close"]),
                    "exchange_timestamp": ts.tz_localize(None).to_pydatetime(),
                    "volume_traded": 100,
                    "oi": float(row["oi"]),
                }
            ]
        )
        harness.step()
        harness.clock.set(ts + pd.Timedelta(seconds=65))
        harness.step()

        # The candle was still aggregated and stored despite the failing channel.
        assert harness.runtime.engine.snapshot()["NSE:TEST"]["bars"] >= 51
    finally:
        harness.runtime.shutdown()
