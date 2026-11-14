"""Stale feed: detect, alert once, reconnect in a controlled way."""

from __future__ import annotations

import pandas as pd
import pytest

from fakes.fake_kite import FakeTicker

from .support import build_harness

pytestmark = pytest.mark.e2e


def test_no_data_since_open_is_reported(tmp_path) -> None:
    harness = build_harness(tmp_path, feed_stale_seconds=60)
    try:
        harness.start_feed()
        harness.step()
        harness.clock.set(pd.Timestamp("2026-11-16 09:16", tz="Asia/Kolkata"))
        harness.step()
        harness.runtime.hub.flush(timeout=5)
        assert any(
            "No market data received since the 09:15 open" in text
            for text in harness.notifier.texts
        )
    finally:
        harness.runtime.shutdown()


def test_stale_feed_triggers_one_reconnect(tmp_path) -> None:
    harness = build_harness(tmp_path, feed_stale_seconds=60)
    try:
        first = harness.start_feed()
        harness.step()
        harness.clock.set(pd.Timestamp("2026-11-16 09:16", tz="Asia/Kolkata"))
        harness.step()

        harness.clock.set(pd.Timestamp("2026-11-16 09:18", tz="Asia/Kolkata"))
        harness.step()
        harness.runtime.hub.flush(timeout=5)

        assert harness.runtime.worker.reconnects == 1
        assert any("Feed stale" in text for text in harness.notifier.texts)
        assert first.stopped is True
        assert FakeTicker.instances[-1] is not first  # a fresh socket was created
        assert FakeTicker.instances[-1].connected is True
    finally:
        harness.runtime.shutdown()


def test_reconnect_is_not_hammered(tmp_path) -> None:
    harness = build_harness(tmp_path, feed_stale_seconds=60)
    try:
        harness.start_feed()
        harness.step()
        for minute in (16, 18, 19, 20):
            harness.clock.set(pd.Timestamp(f"2026-11-16 09:{minute}", tz="Asia/Kolkata"))
            harness.step()
        assert harness.runtime.worker.reconnects == 1  # cooldown respected
    finally:
        harness.runtime.shutdown()


def test_watchdog_is_quiet_outside_the_session(tmp_path) -> None:
    harness = build_harness(tmp_path, feed_stale_seconds=60)
    try:
        harness.start_feed()
        harness.step()
        harness.clock.set(pd.Timestamp("2026-11-16 20:00", tz="Asia/Kolkata"))
        harness.step()
        harness.runtime.hub.flush(timeout=5)
        assert not any("Feed stale" in text for text in harness.notifier.texts)
        assert not any("No market data" in text for text in harness.notifier.texts)
    finally:
        harness.runtime.shutdown()


def test_health_endpoint_reflects_staleness(tmp_path) -> None:
    harness = build_harness(tmp_path, feed_stale_seconds=60)
    try:
        harness.start_feed()
        harness.step()
        harness.clock.set(pd.Timestamp("2026-11-16 09:18", tz="Asia/Kolkata"))
        harness.step()
        status = harness.runtime.health_status()
        assert status["healthy"] is False
        assert status["status"] in ("stale_feed", "auth_failed")
    finally:
        harness.runtime.shutdown()
