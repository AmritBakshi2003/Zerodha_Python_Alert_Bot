"""Known-value tests for the pure chart maths."""

from __future__ import annotations

import pandas as pd
import pytest

from kite_alerts.config.settings import StrategyParams
from kite_alerts.core.clock import IST
from kite_alerts.indicators import ema, macd, rsi
from kite_alerts.ui.chart.service import indicator_frame
from kite_alerts.ui.chart.symbols import (
    NSE_INDEX_MAP,
    delayed_symbols,
    resolve_nse_symbol,
)
from kite_alerts.ui.chart.transforms import (
    age_text,
    change_percent,
    from_frame,
    heikin_ashi,
    ist_label,
    resample_candles,
    to_frame,
)
from kite_alerts.ui.chart.types import Candle
from unit.ui_support_c3 import make_candles, make_frame


class TestTimeAndCandles:
    def test_true_epoch_round_trip_in_ist(self) -> None:
        stamp = pd.Timestamp("2026-11-16 09:15", tz=IST)
        frame = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]},
            index=pd.DatetimeIndex([stamp]),
        )
        candles = from_frame(frame)
        assert candles[0].time == int(stamp.timestamp())
        assert ist_label(candles[0].time) == "2026-11-16 09:15"
        restored = to_frame(candles)
        assert restored.index[0] == stamp

    def test_age_text_is_plain_language(self) -> None:
        assert age_text(0) == "less than a minute old"
        assert age_text(120) == "2 min old"
        assert age_text(7200) == "2 h old"
        assert age_text(2 * 86400) == "2 d old"


class TestResampling:
    def test_three_minute_buckets_use_ohlcv_rules(self) -> None:
        candles = (
            Candle(1_699_999_920, 100, 102, 99, 101, 10),
            Candle(1_699_999_980, 101, 104, 100, 103, 20),
            Candle(1_700_000_040, 103, 105, 102, 104, 30),
            Candle(1_700_000_100, 104, 106, 103, 105, 40),
        )
        result = resample_candles(candles, 3)
        assert len(result) == 2
        first = result[0]
        assert (first.open, first.high, first.low, first.close, first.volume) == (
            100,
            105,
            99,
            104,
            60,
        )
        second = result[1]
        assert (second.open, second.high, second.low, second.close, second.volume) == (
            104,
            106,
            103,
            105,
            40,
        )

    def test_partial_last_bucket_is_kept(self) -> None:
        candles = make_candles(periods=120)
        result = resample_candles(candles, 15)
        assert len(result) == 8
        assert result[-1].time == candles[-1].time - candles[-1].time % (15 * 60)


class TestHeikinAshi:
    def test_first_two_known_values(self) -> None:
        candles = (
            Candle(0, 100, 110, 90, 105),
            Candle(60, 105, 115, 100, 110),
        )
        result = heikin_ashi(candles)
        first = result[0]
        assert first["open"] == pytest.approx(102.5)
        assert first["close"] == pytest.approx(101.25)
        assert first["high"] == pytest.approx(110)
        assert first["low"] == pytest.approx(90)
        second = result[1]
        assert second["open"] == pytest.approx((102.5 + 101.25) / 2)
        assert second["close"] == pytest.approx(107.5)
        assert second["high"] == pytest.approx(115)
        assert second["low"] == pytest.approx(100)

    def test_empty_series(self) -> None:
        assert heikin_ashi(()) == ()


class TestSymbolMapping:
    def test_nse_equity_and_indices(self) -> None:
        assert resolve_nse_symbol("NSE:RELIANCE").provider_symbol == "RELIANCE.NS"
        assert resolve_nse_symbol("nse:reliance").provider_symbol == "RELIANCE.NS"
        assert resolve_nse_symbol("NSE:NIFTY 50").provider_symbol == "^NSEI"
        assert resolve_nse_symbol("NSE:BANK NIFTY").provider_symbol == "^NSEBANK"
        assert resolve_nse_symbol("NSE:NIFTY").provider_symbol == "^NSEI"

    def test_mapping_table_is_explicit(self) -> None:
        assert NSE_INDEX_MAP["NSE:BANK NIFTY"] == ("^NSEBANK", "BANK NIFTY")

    def test_nfo_is_refused_with_a_reason(self) -> None:
        info = resolve_nse_symbol("NFO:NIFTY26NOVFUT")
        assert info.supported is False
        assert "NFO" in info.reason
        assert "not available" in info.reason

    def test_non_nse_is_refused(self) -> None:
        info = resolve_nse_symbol("NASDAQ:AAPL")
        assert info.supported is False
        assert "NSE" in info.reason

    def test_plain_crypto_tickers_are_valid_but_urls_are_not(self) -> None:
        from kite_alerts.ui.chart.symbols import is_safe_symbol

        assert is_safe_symbol("BTC")
        assert is_safe_symbol("ETH")
        assert is_safe_symbol("NSE:RELIANCE")
        assert not is_safe_symbol("http://evil.example")
        assert not is_safe_symbol("../secret")

    def test_delayed_symbols_keep_order_and_drop_duplicates(self) -> None:
        result = delayed_symbols(("NSE:TEST", "NSE:TEST", "NSE:NIFTY 50", "NFO:FUT"))
        assert [info.symbol for info in result] == ["NSE:TEST", "NSE:NIFTY 50", "NFO:FUT"]
        assert result[-1].supported is False


class TestIndicatorParity:
    def test_chart_indicators_match_the_bot_functions(self) -> None:
        frame = make_frame()
        params = StrategyParams()
        result = indicator_frame(frame, params)
        pd.testing.assert_series_equal(
            result["ema_fast"],
            ema(frame["close"], params.ema_fast, seed=params.indicator_seed),
            check_names=False,
        )
        pd.testing.assert_series_equal(
            result["ema_slow"],
            ema(frame["close"], params.ema_slow, seed=params.indicator_seed),
            check_names=False,
        )
        pd.testing.assert_series_equal(
            result["rsi"],
            rsi(frame["close"], params.rsi_period, seed=params.indicator_seed),
            check_names=False,
        )
        line, signal, histogram = macd(
            frame["close"],
            params.macd_fast,
            params.macd_slow,
            params.macd_signal,
            seed=params.indicator_seed,
        )
        pd.testing.assert_series_equal(result["macd"], line, check_names=False)
        pd.testing.assert_series_equal(result["macd_signal"], signal, check_names=False)
        pd.testing.assert_series_equal(result["macd_hist"], histogram, check_names=False)

    def test_change_percent_uses_latest_session_first_close(self) -> None:
        frame = make_frame(periods=10)
        candles = from_frame(frame)
        assert change_percent(candles) == pytest.approx(
            (candles[-1].close - candles[0].close) / candles[0].close * 100
        )
        assert change_percent(candles[:1]) is None
