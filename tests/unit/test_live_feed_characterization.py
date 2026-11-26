"""Characterization tests for the F12 live-feed module split (P5).

These record the exact public payloads and helper behaviour of the types and
helpers that are moved into `ui/live_feed_types.py` / `ui/live_feed_bootstrap.py`.
They pass before and after the move: their job is to prove the refactor is
behaviour-preserving, not to find a bug.
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from kite_alerts.core.clock import IST
from kite_alerts.data.candle_builder import Candle
from kite_alerts.ui.live_feed import (
    STATE_CONNECTING,
    STATE_LIVE,
    FeedAvailability,
    FeedStatus,
    LiveTick,
    SSEEvent,
    TokenRegistry,
    _backoff_delay,
    _candle_dict,
)


def test_feed_status_payload_is_recorded() -> None:
    status = FeedStatus(
        state=STATE_LIVE,
        subscribed=3,
        reconnect_count=2,
        last_tick_age=12.345,
        last_tick_time="09:16:00",
        latency_ms=45.67,
        reason="ok",
    )
    assert status.as_dict() == {
        "state": "live",
        "subscribed": 3,
        "reconnect_count": 2,
        "last_tick_age": 12.3,
        "last_tick_time": "09:16:00",
        "latency_ms": 45.7,
        "reason": "ok",
    }
    assert FeedStatus(state=STATE_CONNECTING).as_dict()["last_tick_age"] is None


def test_feed_availability_payload_is_recorded() -> None:
    value = FeedAvailability(
        state="live",
        available=True,
        enabled=True,
        badge="LIVE",
        notice="Streaming live ticks from Kite.",
        reason="",
    )
    assert value.as_dict() == {
        "state": "live",
        "available": True,
        "enabled": True,
        "badge": "LIVE",
        "notice": "Streaming live ticks from Kite.",
        "reason": "",
    }


def test_live_tick_latency_and_note_are_recorded() -> None:
    good = LiveTick("NSE:TEST", 111, 100.0, 500, 0.0, 100.0, 100.045)
    assert good.latency_ms == pytest.approx(45.0)
    assert good.latency_note == "45 ms"

    skewed = LiveTick("NSE:TEST", 111, 100.0, 500, 0.0, 100.0, 99.9)
    assert skewed.latency_ms is None
    assert skewed.latency_note == "Your PC clock may be out of sync"


def test_sse_event_payload_is_recorded() -> None:
    event = SSEEvent(event="status", data={"state": "live"}, symbol="NSE:TEST")
    assert event.event == "status"
    assert event.data == {"state": "live"}
    assert event.symbol == "NSE:TEST"


def test_token_registry_round_trip_is_recorded() -> None:
    registry = TokenRegistry()
    registry.register("NSE:TEST", 111)
    assert registry.symbol_for(111) == "NSE:TEST"
    assert registry.token_for("NSE:TEST") == 111
    assert registry.all_tokens() == [111]
    registry.clear()
    assert registry.all_tokens() == []


def test_backoff_schedule_is_recorded() -> None:
    rng = random.Random(42)
    values = [_backoff_delay(n, rng) for n in (1, 2, 3, 4)]
    assert all(1 <= v <= 60 * 1.25 for v in values)
    assert all(v >= 0 for v in values)


def test_candle_dict_payload_is_recorded() -> None:
    candle = Candle(
        symbol="NSE:TEST",
        time=pd.Timestamp("2026-11-16 09:16:00", tz=IST),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=500,
        oi=0.0,
    )
    result = _candle_dict(candle, is_simulated=False)
    assert set(result) == {
        "time", "ist", "open", "high", "low", "close", "volume", "oi", "is_simulated",
    }
    assert result["open"] == 100.0
    assert result["oi"] is None
    assert result["is_simulated"] is False
