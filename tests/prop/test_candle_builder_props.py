"""Property tests: no tick ordering can corrupt a candle or emit duplicates."""

from __future__ import annotations

import pandas as pd
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kite_alerts.core.clock import to_ist_ts
from kite_alerts.data.candle_builder import CandleBuilder

BASE = to_ist_ts("2026-11-16 09:15:00")

tick_strategy = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=6),  # minute offset
        st.integers(min_value=0, max_value=59),  # seconds
        st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
        st.integers(min_value=0, max_value=50_000),  # cumulative volume
    ),
    max_size=120,
)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ticks=tick_strategy)
def test_candle_invariants_hold_for_any_tick_order(ticks) -> None:
    builder = CandleBuilder()
    emitted = []
    for minute, second, price, volume in ticks:
        stamp = BASE + pd.Timedelta(minutes=minute, seconds=second)
        emitted.extend(builder.on_tick("X", stamp, price, volume_cum=volume))
    emitted.extend(builder.close_completed(BASE + pd.Timedelta(minutes=10)))

    times = [candle.time for candle in emitted]
    assert times == sorted(times), "candles must be emitted in time order"
    assert len(set(times)) == len(times), "a minute must never be emitted twice"

    for candle in emitted:
        assert candle.high >= max(candle.open, candle.close)
        assert candle.low <= min(candle.open, candle.close)
        assert candle.high >= candle.low
        assert candle.volume >= 0
        assert candle.close > 0


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ticks=tick_strategy)
def test_aggregation_is_deterministic(ticks) -> None:
    def run() -> list[tuple]:
        builder = CandleBuilder()
        out = []
        for minute, second, price, volume in ticks:
            stamp = BASE + pd.Timedelta(minutes=minute, seconds=second)
            out.extend(builder.on_tick("X", stamp, price, volume_cum=volume))
        out.extend(builder.close_completed(BASE + pd.Timedelta(minutes=10)))
        return [(c.time, c.open, c.high, c.low, c.close, c.volume, c.oi) for c in out]

    # The same ticks must always produce byte-identical candles.
    assert run() == run()


@settings(max_examples=30, deadline=None)
@given(ticks=tick_strategy)
def test_replay_after_reset_does_not_re_emit_closed_minutes(ticks) -> None:
    builder = CandleBuilder()
    first = []
    for minute, second, price, volume in ticks:
        stamp = BASE + pd.Timedelta(minutes=minute, seconds=second)
        first.extend(builder.on_tick("X", stamp, price, volume_cum=volume))
    first.extend(builder.close_completed(BASE + pd.Timedelta(minutes=10)))

    # reset() clears in-progress state but remembers what was already emitted,
    # so a reconnect back-fill can never duplicate an alert for the same minute.
    builder.reset()
    replayed = []
    for minute, second, price, volume in ticks:
        stamp = BASE + pd.Timedelta(minutes=minute, seconds=second)
        replayed.extend(builder.on_tick("X", stamp, price, volume_cum=volume))
    replayed.extend(builder.close_completed(BASE + pd.Timedelta(minutes=10)))

    assert not ({c.time for c in replayed} & {c.time for c in first})


@settings(max_examples=30, deadline=None)
@given(
    price=st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    volume=st.integers(min_value=0, max_value=10**9),
)
def test_single_tick_candle_is_flat(price: float, volume: int) -> None:
    builder = CandleBuilder()
    builder.on_tick("X", BASE + pd.Timedelta(seconds=5), price, volume_cum=volume)
    candle = builder.close_completed(BASE + pd.Timedelta(minutes=1))[0]
    assert candle.open == candle.high == candle.low == candle.close == price
    assert candle.volume == 0
