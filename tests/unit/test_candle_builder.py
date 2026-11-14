"""Candle aggregation: volume, clock-driven close, late and duplicate ticks."""

from __future__ import annotations

import pandas as pd
import pytest

from kite_alerts.core.clock import to_ist_ts
from kite_alerts.data.candle_builder import CandleBuilder
from kite_alerts.observability.metrics import Metrics


def at(hhmmss: str) -> pd.Timestamp:
    return to_ist_ts(f"2026-11-16 {hhmmss}")


@pytest.fixture
def builder(metrics: Metrics) -> CandleBuilder:
    return CandleBuilder(metrics=metrics)


class TestVolume:
    def test_volume_is_the_difference_of_cumulative_volume(self, builder: CandleBuilder) -> None:
        builder.on_tick("X", at("09:15:05"), 100.0, volume_cum=1_000)
        builder.on_tick("X", at("09:15:30"), 101.0, volume_cum=1_250)
        builder.on_tick("X", at("09:15:50"), 100.5, volume_cum=1_400)
        candles = builder.close_completed(at("09:16:00"))
        assert len(candles) == 1
        candle = candles[0]
        assert candle.volume == 400
        assert (candle.open, candle.high, candle.low, candle.close) == (100.0, 101.0, 100.0, 100.5)

    def test_volume_resets_instead_of_going_negative(self, builder: CandleBuilder) -> None:
        builder.on_tick("X", at("09:15:05"), 100.0, volume_cum=50_000)
        builder.on_tick("X", at("09:15:30"), 100.2, volume_cum=100)  # session/feed reset
        candle = builder.close_completed(at("09:16:00"))[0]
        assert candle.volume == 0

    def test_missing_volume_is_zero(self, builder: CandleBuilder) -> None:
        builder.on_tick("X", at("09:15:05"), 100.0, volume_cum=None)
        candle = builder.close_completed(at("09:16:00"))[0]
        assert candle.volume == 0


class TestClockClose:
    def test_candle_closes_on_the_clock_without_a_next_tick(self, builder: CandleBuilder) -> None:
        builder.on_tick("X", at("09:15:05"), 100.0, volume_cum=10)
        assert builder.close_completed(at("09:15:59")) == []
        candles = builder.close_completed(at("09:16:00"))
        assert [c.time for c in candles] == [at("09:15")]

    def test_new_minute_tick_also_closes_the_previous_candle(self, builder: CandleBuilder) -> None:
        builder.on_tick("X", at("09:15:05"), 100.0, volume_cum=10)
        candles = builder.on_tick("X", at("09:16:01"), 101.0, volume_cum=20)
        assert len(candles) == 1
        assert candles[0].time == at("09:15")

    def test_quiet_minute_emits_nothing(self, builder: CandleBuilder) -> None:
        builder.on_tick("X", at("09:15:05"), 100.0)
        builder.close_completed(at("09:16:00"))
        assert builder.close_completed(at("09:20:00")) == []
        assert builder.pending(at("09:20:00")) == ()

    def test_candle_is_emitted_only_once(self, builder: CandleBuilder) -> None:
        builder.on_tick("X", at("09:15:05"), 100.0)
        first = builder.close_completed(at("09:16:00"))
        second = builder.close_completed(at("09:17:00"))
        assert len(first) == 1
        assert second == []


class TestTickDiscipline:
    def test_late_tick_is_dropped_and_counted(
        self, builder: CandleBuilder, metrics: Metrics
    ) -> None:
        builder.on_tick("X", at("09:16:05"), 100.0)
        emitted = builder.on_tick("X", at("09:15:30"), 999.0)
        assert emitted == []
        assert metrics.get("late_ticks", symbol="X") == 1
        candle = builder.close_completed(at("09:17:00"))[0]
        assert candle.high == 100.0  # the late price never contaminated the candle

    def test_duplicate_tick_is_ignored(self, builder: CandleBuilder, metrics: Metrics) -> None:
        tick = (at("09:15:05"), 100.0, 500)
        builder.on_tick("X", tick[0], tick[1], volume_cum=tick[2])
        builder.on_tick("X", tick[0], tick[1], volume_cum=tick[2])
        candle = builder.close_completed(at("09:16:00"))[0]
        assert candle.volume == 0
        assert metrics.get("duplicate_ticks", symbol="X") == 1

    def test_out_of_order_ticks_within_a_minute_still_bound_the_range(
        self, builder: CandleBuilder
    ) -> None:
        builder.on_tick("X", at("09:15:40"), 105.0)
        builder.on_tick("X", at("09:15:10"), 99.0)
        candle = builder.close_completed(at("09:16:00"))[0]
        assert candle.high == 105.0
        assert candle.low == 99.0
        assert candle.close == 99.0

    def test_open_interest_takes_the_last_value(self, builder: CandleBuilder) -> None:
        builder.on_tick("X", at("09:15:05"), 100.0, oi=1_000)
        builder.on_tick("X", at("09:15:40"), 100.5, oi=1_200)
        candle = builder.close_completed(at("09:16:00"))[0]
        assert candle.oi == 1_200

    def test_symbols_are_independent(self, builder: CandleBuilder) -> None:
        builder.on_tick("A", at("09:15:05"), 10.0)
        builder.on_tick("B", at("09:15:06"), 20.0)
        candles = builder.close_completed(at("09:16:00"))
        assert {c.symbol for c in candles} == {"A", "B"}

    def test_reset_discards_in_progress(self, builder: CandleBuilder) -> None:
        builder.on_tick("X", at("09:15:05"), 100.0)
        builder.reset("X")
        assert builder.close_completed(at("09:16:00")) == []
        assert builder.in_progress() == {}

    def test_thread_safety_smoke(self, builder: CandleBuilder) -> None:
        import threading

        def worker(offset: int) -> None:
            for step in range(200):
                builder.on_tick(
                    "X",
                    at(f"09:{15 + offset:02d}:{step % 60:02d}"),
                    100.0 + step * 0.01,
                    volume_cum=step,
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # No exception and no corruption; state remains readable.
        assert isinstance(builder.in_progress(), dict)
