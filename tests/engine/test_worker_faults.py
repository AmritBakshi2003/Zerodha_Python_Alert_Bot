"""F11 — fault injection around the product path: ``MarketWorker``.

Every test here drives the production worker (built by ``build_runtime`` from
``tests/e2e/support.py``) and asserts the *user-visible* behaviour of a failure:
the run loop keeps going, the drop is counted, an auth failure stops the run
with exactly one message.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from e2e.support import Harness, build_harness, ticks_for_bar
from fakes.fake_kite import TokenException
from kite_alerts.core.models import Tick

SESSION_NOON = pd.Timestamp("2026-11-16 09:16", tz="Asia/Kolkata")


@pytest.fixture
def harness(tmp_path: Any) -> Any:
    built = build_harness(tmp_path)
    try:
        yield built
    finally:
        built.runtime.shutdown()


def _tick(price: float = 100.0) -> Tick:
    return Tick(
        symbol="NSE:TEST",
        token=111,
        ts=pd.Timestamp("2026-11-16 09:16:00", tz="Asia/Kolkata"),
        price=price,
    )


class TestEngineFaults:
    """A raising engine must never end the session (F11)."""

    def _explode(self, harness: Harness) -> list[pd.Timestamp]:
        calls: list[pd.Timestamp] = []

        def explode(candle: Any) -> None:
            calls.append(candle.time)
            raise RuntimeError("rule evaluation blew up")

        harness.runtime.engine.on_candle = explode  # type: ignore[method-assign]
        return calls

    def test_a_clock_closed_candle_does_not_kill_step(self, harness: Harness) -> None:
        calls = self._explode(harness)
        ticker = harness.start_feed()
        harness.step()  # drain the first-connect back-fill command

        ts, row = harness.session.index[0], harness.session.iloc[0]
        harness.clock.set(ts + pd.Timedelta(seconds=1))
        ticker.push(ticks_for_bar(ts, row, cumulative_volume=10_000))
        harness.step(4)

        harness.clock.set(ts + pd.Timedelta(seconds=65))
        assert harness.runtime.worker.step(timeout=0) is True

        assert len(calls) == 1
        assert harness.runtime.metrics.get("engine_errors", symbol="NSE:TEST") == 1
        # The loop still accepts work afterwards.
        harness.runtime.worker.enqueue_tick(_tick())
        assert harness.runtime.worker.step(timeout=0) is True

    def test_a_tick_closed_candle_does_not_kill_step(self, harness: Harness) -> None:
        calls = self._explode(harness)
        ticker = harness.start_feed()
        harness.step()

        ts, row = harness.session.index[0], harness.session.iloc[0]
        harness.clock.set(ts + pd.Timedelta(seconds=1))
        ticker.push(ticks_for_bar(ts, row, cumulative_volume=10_000))
        harness.step(4)

        # The first tick of the *next* minute closes minute 0 inside the builder,
        # which is the other call site of on_candle.
        next_minute = ticks_for_bar(ts + pd.Timedelta(minutes=1), row, cumulative_volume=20_000)[0]
        ticker.push([next_minute])
        harness.runtime.worker.step(timeout=0)

        assert len(calls) == 1
        assert harness.runtime.metrics.get("engine_errors", symbol="NSE:TEST") == 1


class TestQueueFaults:
    def test_a_full_queue_drops_ticks_and_counts_them(self, harness: Harness) -> None:
        worker = harness.runtime.worker
        capacity = worker._queue.maxsize  # type: ignore[attr-defined]
        tick = _tick()
        for _ in range(capacity):
            worker.enqueue_tick(tick)
        assert worker.queue_depth == capacity

        worker.enqueue_tick(tick)  # the drop

        assert worker.queue_depth == capacity
        assert harness.runtime.metrics.get("ticks_dropped") == 1


class TestBackfillFaults:
    def test_an_expired_token_alerts_once_and_stops_the_run(self, harness: Harness) -> None:
        worker = harness.runtime.worker
        harness.runtime.hub.start()
        harness.clock.set(SESSION_NOON)
        harness.fake.history_error = TokenException("token expired")

        worker.request_backfill(first_connect=False)
        worker.step(timeout=0)

        assert worker.auth_failed is True
        assert worker.stop_event.is_set()
        assert "login" in worker.auth_message

        harness.runtime.hub.flush(timeout=5)
        auth_messages = [text for text in harness.notifier.texts if "🔐" in text]
        assert len(auth_messages) == 1

        # A second report (e.g. another symbol failing) is swallowed.
        worker._fail_auth("Kite rejected the token again")
        harness.runtime.hub.flush(timeout=5)
        assert len([text for text in harness.notifier.texts if "🔐" in text]) == 1

    def test_outside_the_session_no_history_call_is_made(self, harness: Harness) -> None:
        worker = harness.runtime.worker
        harness.fake.history_calls.clear()
        harness.clock.set(pd.Timestamp("2026-11-16 08:00", tz="Asia/Kolkata"))

        worker.request_backfill(first_connect=True)
        worker.step(timeout=0)

        assert harness.fake.history_calls == []

    def test_a_network_failure_is_counted_and_the_run_continues(self, harness: Harness) -> None:
        worker = harness.runtime.worker
        harness.clock.set(SESSION_NOON)
        harness.fake.history_error = ConnectionError("network down")

        worker.request_backfill(first_connect=False)
        worker.step(timeout=0)

        assert worker.auth_failed is False
        assert not worker.stop_event.is_set()
        assert harness.runtime.metrics.get("backfill_failures", symbol="NSE:TEST") == 1

    def test_an_empty_frame_is_counted(self, harness: Harness) -> None:
        worker = harness.runtime.worker
        harness.clock.set(SESSION_NOON)
        harness.fake.history_data[111] = harness.session.iloc[0:0]

        worker.request_backfill(first_connect=True)
        worker.step(timeout=0)

        assert harness.runtime.metrics.get("backfill_empty", symbol="NSE:TEST") == 1


class TestHeartbeatFaults:
    def test_heartbeat_reports_counters_and_last_tick(self, tmp_path: Any) -> None:
        from kite_alerts.config.settings import ObservabilitySettings

        observability = ObservabilitySettings(
            http_enabled=False, heartbeat_minutes=1, log_dir=tmp_path / "logs"
        )
        harness = build_harness(tmp_path, observability=observability)
        try:
            worker = harness.runtime.worker
            harness.runtime.hub.start()
            start = pd.Timestamp("2026-11-16 09:16", tz="Asia/Kolkata")
            worker._maybe_heartbeat(start)
            worker._maybe_heartbeat(start + pd.Timedelta(seconds=30))
            harness.runtime.hub.flush(timeout=5)
            assert not [text for text in harness.notifier.texts if "💓" in text]

            worker._maybe_heartbeat(start + pd.Timedelta(seconds=61))
            harness.runtime.hub.flush(timeout=5)
            beats = [text for text in harness.notifier.texts if "💓" in text]

            assert len(beats) == 1
            assert "09:17 IST" in beats[0]
            assert "candles" in beats[0]
            assert "alerts" in beats[0]
            assert "reconnects 0" in beats[0]
        finally:
            harness.runtime.shutdown()

    def test_heartbeat_is_off_by_default_and_outside_the_session(self, harness: Harness) -> None:
        harness.runtime.worker._maybe_heartbeat(SESSION_NOON)
        harness.runtime.hub.flush(timeout=5)
        assert not [text for text in harness.notifier.texts if "💓" in text]


class TestReconnectFaults:
    def test_reconnect_restarts_the_feed_and_requests_a_backfill(self, harness: Harness) -> None:
        old_ticker = harness.start_feed()
        harness.step()
        worker = harness.runtime.worker

        worker._reconnect()

        assert worker.reconnects == 1
        assert old_ticker.stopped is True
        assert old_ticker.closed is True
        assert harness.ticker is not old_ticker
        assert worker.queue_depth >= 1
        assert harness.runtime.metrics.get("feed_reconnects") == 1

    def test_a_failing_restart_is_counted_and_does_not_raise(self, harness: Harness) -> None:
        class ExplodingFeed:
            def stop(self) -> None:
                raise RuntimeError("stop failed")

            def start(self) -> None:
                raise RuntimeError("start failed")

        worker = harness.runtime.worker
        worker.attach_feed(ExplodingFeed())

        worker._reconnect()

        assert worker.reconnects == 1
        assert harness.runtime.metrics.get("reconnect_errors") == 1


class TestRunLoopFaults:
    def test_run_survives_a_step_exception(self, harness: Harness) -> None:
        worker = harness.runtime.worker
        calls = {"count": 0}

        def flaky_step(timeout: float | None = None) -> bool:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("step blew up")
            worker.stop_event.set()
            return True

        worker.step = flaky_step  # type: ignore[method-assign]
        worker.run()
        assert calls["count"] == 2
        assert harness.runtime.metrics.get("worker_errors") == 1

    def test_health_snapshot_is_json_ready(self, harness: Harness) -> None:
        harness.clock.set(SESSION_NOON)
        worker = harness.runtime.worker
        health = worker.health()

        assert health["in_session"] is True
        assert health["auth_failed"] is False
        assert health["queue_depth"] == 0
        assert health["engine"] == {"symbols": 1, "bars": 50}
        assert health["in_progress_candles"] == {}
