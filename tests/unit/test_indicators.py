"""Known-value tests for EMA, RSI and MACD.

The reference numbers are the published Wilder/TA-Lib convention (SMA-seeded),
which is what charting platforms and TA-Lib compute. ``seed="first"`` is the
prototype's pandas recursion and is asserted separately so the documented
deviation cannot drift silently.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from kite_alerts.indicators import ema, macd, opening_range, rsi

# Classic 20-bar series from the published Wilder RSI worked example.
WILDER_SERIES = pd.Series(
    [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
        46.00,
        46.03,
        46.41,
        46.22,
        45.64,
    ]
)
#: Published value for this series at bar 14 (TA-Lib / StockCharts): 70.46.
WILDER_RSI_14 = 70.464135


class TestEma:
    def test_sma_seed_matches_reference_convention(self) -> None:
        series = pd.Series([float(value) for value in range(1, 25)])
        result = ema(series, 9, seed="sma")
        # A linear ramp decays to an exact lag of (period - 1) / 2 = 4.
        assert result.iloc[8] == pytest.approx(5.0)
        assert result.iloc[23] == pytest.approx(20.0)

    def test_first_seed_reproduces_prototype(self) -> None:
        series = pd.Series([float(value) for value in range(1, 25)])
        assert ema(series, 9, seed="first").iloc[23] == pytest.approx(20.023612, abs=1e-6)

    def test_nan_before_period(self) -> None:
        series = pd.Series([1.0, 2.0, 3.0, 4.0])
        assert ema(series, 3, seed="sma").iloc[:2].isna().all()
        assert not pd.isna(ema(series, 3, seed="sma").iloc[2])

    def test_seeds_converge(self) -> None:
        series = pd.Series(np.linspace(100, 120, 200) + np.sin(np.arange(200)))
        difference = (ema(series, 9) - ema(series, 9, seed="sma")).abs()
        assert difference.iloc[-1] < 1e-9

    @pytest.mark.parametrize("bad", [0, -1])
    def test_invalid_period(self, bad: int) -> None:
        with pytest.raises(ValueError):
            ema(pd.Series([1.0, 2.0]), bad)

    def test_invalid_seed(self) -> None:
        with pytest.raises(ValueError, match="seed"):
            ema(pd.Series([1.0, 2.0]), 2, seed="wild")


class TestRsi:
    def test_sma_seed_matches_published_wilder_value(self) -> None:
        result = rsi(WILDER_SERIES, 14, seed="sma")
        assert result.iloc[14] == pytest.approx(WILDER_RSI_14, abs=1e-5)

    def test_first_seed_is_the_prototype_value(self) -> None:
        result = rsi(WILDER_SERIES, 14, seed="first")
        assert result.iloc[14] == pytest.approx(50.657415, abs=1e-5)

    def test_documented_deviation_between_seeds(self) -> None:
        """The prototype's seed differs materially early on: this is why it is opt-in."""
        first = rsi(WILDER_SERIES, 14, seed="first").iloc[14]
        reference = rsi(WILDER_SERIES, 14, seed="sma").iloc[14]
        assert abs(first - reference) > 15

    def test_bounded_and_monotonic_cases(self) -> None:
        up = pd.Series(np.arange(1, 60, dtype=float))
        down = pd.Series(np.arange(60, 1, -1, dtype=float))
        assert rsi(up, 14, seed="sma").iloc[-1] == pytest.approx(100.0)
        assert rsi(down, 14, seed="sma").iloc[-1] == pytest.approx(0.0)

    def test_flat_market_is_documented_per_seed(self) -> None:
        flat = pd.Series([100.0] * 30)
        # Prototype semantics: no losses means a maximal reading.
        assert rsi(flat, 14, seed="first").iloc[-1] == pytest.approx(100.0)
        # TA-Lib convention: no information, neutral mid-point.
        assert rsi(flat, 14, seed="sma").iloc[-1] == pytest.approx(50.0)

    def test_results_stay_within_bounds(self) -> None:
        rng = np.random.default_rng(3)
        series = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
        for seed in ("first", "sma"):
            values = rsi(series, 14, seed=seed).dropna()
            assert values.between(0, 100).all()

    def test_short_series_is_all_nan(self) -> None:
        assert rsi(pd.Series([1.0, 2.0, 3.0]), 14, seed="sma").isna().all()

    def test_invalid_period(self) -> None:
        with pytest.raises(ValueError):
            rsi(pd.Series([1.0, 2.0]), 1)


class TestMacd:
    def test_reference_values(self) -> None:
        series = pd.Series([100 + (i % 7) * 0.5 + i * 0.1 for i in range(60)])
        line, signal, hist = macd(series, 12, 26, 9, seed="sma")
        assert line.iloc[40] == pytest.approx(0.787108, abs=1e-5)
        assert signal.iloc[40] == pytest.approx(0.721071, abs=1e-5)
        assert hist.iloc[40] == pytest.approx(0.066037, abs=1e-5)
        assert line.iloc[59] == pytest.approx(0.630500, abs=1e-5)

    def test_histogram_is_line_minus_signal(self) -> None:
        series = pd.Series(np.linspace(10, 30, 80))
        line, signal, hist = macd(series)
        pd.testing.assert_series_equal(hist, line - signal)

    def test_prototype_seed_values(self) -> None:
        series = pd.Series([100 + (i % 7) * 0.5 + i * 0.1 for i in range(60)])
        line, _, _ = macd(series, 12, 26, 9, seed="first")
        assert line.iloc[59] == pytest.approx(0.628731, abs=1e-6)

    def test_fast_must_be_below_slow(self) -> None:
        with pytest.raises(ValueError, match="fast"):
            macd(pd.Series(np.arange(40, dtype=float)), 26, 12)


class TestOpeningRange:
    def test_range_not_complete_before_window_closes(self, calendar) -> None:
        index = pd.date_range("2026-11-16 09:15", periods=10, freq="min", tz="Asia/Kolkata")
        frame = pd.DataFrame(
            {"high": np.arange(10) + 101.0, "low": np.arange(10) + 100.0},
            index=index,
        )
        assert opening_range(frame, 15, session_open=calendar.window.open_time) is None

    def test_range_uses_only_the_first_minutes(self, calendar) -> None:
        index = pd.date_range("2026-11-16 09:15", periods=20, freq="min", tz="Asia/Kolkata")
        frame = pd.DataFrame(
            {"high": np.arange(20) + 101.0, "low": np.arange(20) + 100.0},
            index=index,
        )
        result = opening_range(frame, 15, session_open=calendar.window.open_time)
        assert result is not None
        assert result.high == pytest.approx(115.0)  # bars 09:15..09:29 -> high 101..115
        assert result.low == pytest.approx(100.0)
        assert result.bars == 15

    def test_empty_and_late_start(self, calendar) -> None:
        assert opening_range(pd.DataFrame(columns=["high", "low"]), 15) is None

        late = pd.date_range("2026-11-16 11:00", periods=5, freq="min", tz="Asia/Kolkata")
        frame = pd.DataFrame({"high": [1.0] * 5, "low": [0.5] * 5}, index=late)
        assert opening_range(frame, 15, session_open=calendar.window.open_time) is None

    def test_invalid_minutes(self) -> None:
        with pytest.raises(ValueError):
            opening_range(pd.DataFrame(), 0)
