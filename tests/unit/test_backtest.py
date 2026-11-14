"""Replay/backtest: same engine, forward returns, CSV loading, report."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import make_frame
from fakes.fake_kite import FakeKite
from kite_alerts.backtest.replay import (
    build_report,
    build_result,
    forward_return,
    load_csv,
    replay_frame,
    replay_kite_history,
)
from kite_alerts.config.settings import ResilienceSettings, StrategyParams
from kite_alerts.core.models import Side
from kite_alerts.demo import synthetic_day


class TestReplay:
    def test_reproduces_the_demo_alerts(self) -> None:
        data = synthetic_day()
        alerts = replay_frame(
            "DEMO:FUT", data, StrategyParams(ema_cross_lookback=5), warmup_bars=50
        )
        assert [alert.side.value for alert in alerts] == ["SELL", "SELL", "BUY"]
        assert [alert.ts.strftime("%H:%M") for alert in alerts] == ["09:34", "09:49", "10:07"]

    def test_empty_frame(self) -> None:
        assert replay_frame("X", pd.DataFrame(), StrategyParams()) == []

    def test_unused_warmup_returns_no_alerts(self) -> None:
        frame = make_frame(np.linspace(100, 101, 20))
        assert replay_frame("X", frame, StrategyParams(), warmup_bars=20) == []


class TestForwardReturn:
    def test_buy_and_sell_signs(self) -> None:
        frame = make_frame([100.0, 101.0, 102.0, 103.0, 104.0, 110.0])
        ts = frame.index[0]
        assert forward_return(frame, ts, Side.BUY, 5) == pytest.approx(10.0)
        assert forward_return(frame, ts, Side.SELL, 5) == pytest.approx(-10.0)

    def test_missing_future_data(self) -> None:
        frame = make_frame([100.0, 101.0])
        assert forward_return(frame, frame.index[0], Side.BUY, 30) is None

    def test_unknown_timestamp(self) -> None:
        frame = make_frame([100.0, 101.0])
        assert (
            forward_return(frame, pd.Timestamp("2026-11-16 15:00", tz="Asia/Kolkata"), Side.BUY, 5)
            is None
        )

    def test_zero_entry_price(self) -> None:
        frame = make_frame([100.0, 101.0])
        frame.loc[frame.index[0], "close"] = 0.0
        assert forward_return(frame, frame.index[0], Side.BUY, 1) is None


class TestReport:
    def test_report_is_labelled_analysis_only(self) -> None:
        data = synthetic_day()
        alerts = replay_frame("DEMO:FUT", data, StrategyParams(), warmup_bars=50)
        result = build_result("DEMO:FUT", data, alerts)
        report = build_report(result, params=StrategyParams())
        assert "ANALYSIS ONLY" in report
        assert "not a performance guarantee" in report
        assert "BUY 1" in report
        assert "SELL 2" in report
        assert "+5m:" in report
        assert result.buys == 1
        assert result.sells == 2

    def test_report_handles_missing_forward_data(self) -> None:
        frame = make_frame([100.0, 101.0])
        alerts = replay_frame("X", frame, StrategyParams(), warmup_bars=2)
        result = build_result("X", frame, alerts)
        report = build_report(result)
        assert "no forward data" in report or "n/a" in report


class TestCsv:
    def test_column_aliases(self, tmp_path: Path) -> None:
        path = tmp_path / "candles.csv"
        pd.DataFrame(
            {
                "date": ["2026-11-16 09:15", "2026-11-16 09:16"],
                "open": [100, 101],
                "high": [101, 102],
                "low": [99, 100],
                "close": [100.5, 101.5],
                "volume_traded": [10, 20],
                "open_interest": [1000, 1100],
            }
        ).to_csv(path, index=False)
        frame = load_csv(path, symbol="NSE:TEST")
        assert str(frame.index.tz) in ("Asia/Kolkata", "IST")
        assert frame["volume"].iloc[0] == 10
        assert frame["oi"].iloc[0] == 1000

    def test_missing_columns(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.csv"
        pd.DataFrame({"a": [1]}).to_csv(path, index=False)
        with pytest.raises(ValueError, match="time"):
            load_csv(path, symbol="X")

        path2 = tmp_path / "bad2.csv"
        pd.DataFrame({"time": ["2026-11-16 09:15"], "open": [1], "high": [1], "low": [1]}).to_csv(
            path2, index=False
        )
        with pytest.raises(ValueError, match="close"):
            load_csv(path2, symbol="X")

    def test_optional_columns_default(self, tmp_path: Path) -> None:
        path = tmp_path / "min.csv"
        pd.DataFrame(
            {
                "time": ["2026-11-16 09:15"],
                "open": [1],
                "high": [1],
                "low": [1],
                "close": [1],
            }
        ).to_csv(path, index=False)
        frame = load_csv(path, symbol="X")
        assert frame["volume"].iloc[0] == 0
        assert frame["oi"].iloc[0] == 0.0


class TestKiteReplay:
    def test_replays_history_from_kite(self) -> None:
        from kite_alerts.core.clock import FrozenClock

        frame = synthetic_day()
        fake = FakeKite(history={111: frame})
        result = replay_kite_history(
            fake,
            symbol="NSE:TEST",
            token=111,
            params=StrategyParams(),
            days=10,
            clock=FrozenClock("2026-11-16 16:00:00"),
        )
        assert result.bars > 100
        assert [alert.side.value for alert in result.alerts] == ["SELL", "SELL", "BUY"]

    def test_empty_history_raises(self) -> None:
        fake = FakeKite()
        with pytest.raises(ValueError, match="no historical candles"):
            replay_kite_history(fake, symbol="X", token=1, params=StrategyParams(), days=1)


def test_resilience_settings_defaults_are_used() -> None:
    assert ResilienceSettings().history_rate_limit_per_sec == 3.0
