"""Adapters, watchlist persistence, clock and signature/alert isolation."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from kite_alerts.core.calendar import TradingCalendar
from kite_alerts.core.clock import IST
from kite_alerts.ui.chart.resilience import ProviderCache
from kite_alerts.ui.chart.sources import (
    CryptoShowcaseSource,
    DelayedNSESource,
    DemoSource,
    KiteLiveSource,
    _delay_seconds,
    _normalise_frame,
)
from kite_alerts.ui.chart.types import Candle, ChartError, SourceError
from kite_alerts.ui.live_feed import (
    STATE_DISCONNECTED,
    STATE_LIVE,
    FeedStatus,
)
from unit.ui_support_c3 import make_chart_context, make_frame


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


def kraken_payload(pair: str = "XXBTZUSD", rows: int = 10):
    base = 1_699_999_920
    return {
        "error": [],
        "result": {
            pair: [
                [
                    base + index * 60,
                    100 + index,
                    101 + index,
                    99 + index,
                    100.5 + index,
                    0,
                    10 + index,
                    5,
                ]
                for index in range(rows)
            ],
            "last": base + rows * 60,
        },
    }


class TestDelayedNSE:
    def test_maps_symbol_and_resamples_three_minutes(self) -> None:
        calls: list[tuple[str, int]] = []

        def history(provider_symbol: str, interval: int):
            calls.append((provider_symbol, interval))
            return make_frame(periods=120)

        cache = ProviderCache(ttl=1000.0, clock=lambda: 0.0, jitter=lambda: 0.0)
        source = DelayedNSESource(
            history=history,
            cache=cache,
            now=lambda: 1_700_000_000.0,
        )
        result = source.fetch("NSE:TEST", 3, None)
        assert calls == [("TEST.NS", 1)]
        assert len(result.candles) == 40
        assert result.provider_interval == 1
        assert result.supports_oi is False
        assert result.delay_seconds is not None
        source.fetch("NSE:TEST", 3, None)
        assert len(calls) == 1  # coalesced/cached, not a second provider call

    def test_nfo_is_refused_before_any_network_call(self) -> None:
        source = DelayedNSESource(history=lambda *_: pytest.fail("must not fetch"))
        with pytest.raises(SourceError) as caught:
            source.fetch("NFO:NIFTY26NOVFUT", 5, None)
        assert "NFO" in caught.value.message

    def test_provider_exception_is_user_facing_and_hides_details(self) -> None:
        def history(*_):
            raise RuntimeError("https://secret.example/path")

        source = DelayedNSESource(history=history, cache=ProviderCache(ttl=0, jitter=lambda: 0))
        with pytest.raises(SourceError) as caught:
            source.fetch("NSE:TEST", 5, None)
        assert "temporarily unavailable" in caught.value.message
        assert "secret.example" not in caught.value.message

    def test_empty_history_is_a_plain_error(self) -> None:
        source = DelayedNSESource(
            history=lambda *_: make_frame(periods=0),
            cache=ProviderCache(ttl=0, jitter=lambda: 0),
        )
        with pytest.raises(SourceError) as caught:
            source.fetch("NSE:TEST", 5, None)
        assert "no candles" in caught.value.message.lower()


class TestCryptoShowcase:
    def test_parses_kraken_rows_and_resamples(self) -> None:
        calls = []

        def http_get(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(kraken_payload(rows=12))

        source = CryptoShowcaseSource(
            http_get=http_get,
            cache=ProviderCache(ttl=1000, clock=lambda: 0.0, jitter=lambda: 0.0),
            now=lambda: 1_700_000_720.0,
        )
        result = source.fetch("BTC", 3, None)
        assert len(result.candles) == 4
        assert calls[0][0] == "https://api.kraken.com/0/public/OHLC"
        assert calls[0][1]["params"] == {"pair": "XBTUSD", "interval": 1}
        assert result.delay_seconds is not None

    def test_accepts_the_provider_specific_pair_key(self) -> None:
        payload = {"error": [], "result": {"XXBTZUSD": kraken_payload()["result"]["XXBTZUSD"]}}
        source = CryptoShowcaseSource(
            http_get=lambda *_, **__: FakeResponse(payload),
            cache=ProviderCache(ttl=1000, clock=lambda: 0.0),
        )
        assert len(source.fetch("BTC", 5, None).candles) == 10

    def test_rate_limit_and_unknown_payload_are_plain(self) -> None:
        source = CryptoShowcaseSource(
            http_get=lambda *_, **__: FakeResponse({}, status_code=429),
            cache=ProviderCache(ttl=0),
        )
        with pytest.raises(SourceError) as caught:
            source.fetch("BTC", 1, None)
        assert "rate limited" in caught.value.message.lower()

        source = CryptoShowcaseSource(
            http_get=lambda *_, **__: FakeResponse({"error": ["bad"], "result": {}}),
            cache=ProviderCache(ttl=0),
        )
        with pytest.raises(SourceError):
            source.fetch("BTC", 1, None)

    def test_only_btc_and_eth_are_allowed(self) -> None:
        source = CryptoShowcaseSource(http_get=lambda *_, **__: pytest.fail("no call"))
        with pytest.raises(SourceError):
            source.fetch("DOGE", 1, None)


class TestDemoAndKite:
    def test_demo_uses_synthetic_day_and_has_oi(self) -> None:
        source = DemoSource()
        assert source.enabled is True
        assert [info.symbol for info in source.symbols(None)] == ["DEMO:FUT"]
        result = source.fetch("DEMO:FUT", 5, None)
        assert len(result.candles) == 31
        assert result.supports_oi is True

    def test_kite_is_visible_but_disabled_and_never_fetches(self) -> None:
        source = KiteLiveSource()
        assert source.enabled is False
        with pytest.raises(SourceError) as caught:
            source.fetch("NSE:TEST", 1, None)
        assert "later update" in caught.value.message

    def test_kite_live_source_enabled_and_badge_properties(self) -> None:
        source = KiteLiveSource()
        source.enabled = True
        assert source.enabled is True
        source.badge = "CUSTOM"
        assert source.badge == "CUSTOM"

        # Using feed provider
        class FakeFeed:
            def __init__(self, state=STATE_LIVE, is_sim=False, reason=""):
                self._st = state
                self.is_simulated = is_sim
                self._rs = reason

            def status(self):
                return FeedStatus(state=self._st, reason=self._rs)

        feed = FakeFeed(state=STATE_LIVE)
        s2 = KiteLiveSource(feed_provider=lambda: feed)
        assert s2.enabled is True
        assert s2.badge == "LIVE"

        feed._st = STATE_DISCONNECTED
        assert s2.enabled is False

        feed_sim = FakeFeed(state=STATE_LIVE, is_sim=True)
        s3 = KiteLiveSource(feed_provider=lambda: feed_sim)
        assert s3.badge == "SIMULATED"
        syms = s3.symbols(None)
        assert len(syms) > 0
        assert syms[0].symbol == "NSE:SIMULATED"

    def test_kite_live_source_fetch_with_feed(self) -> None:
        class FakeFeedWithData:
            def __init__(self):
                self.is_simulated = False
                self._candles = [
                    {
                        "time": 1700000000,
                        "open": 100.0,
                        "high": 105.0,
                        "low": 99.0,
                        "close": 102.0,
                        "volume": 10,
                        "oi": 50.0,
                    },
                    {
                        "time": 1700000060,
                        "open": 102.0,
                        "high": 106.0,
                        "low": 101.0,
                        "close": 104.0,
                        "volume": 15,
                        "oi": 60.0,
                    },
                ]

            def status(self):
                return FeedStatus(state=STATE_LIVE, latency_ms=5.0)

            def bootstrap_data_for(self, symbol: str):
                return self._candles

        feed = FakeFeedWithData()
        source = KiteLiveSource(feed_provider=lambda: feed)
        assert source.enabled is True
        res = source.fetch("NSE:INFY", 1, None)
        assert len(res.candles) == 2
        assert res.candles[0].open == 100.0
        assert res.supports_oi is True

        # Test empty candles raises waiting_for_ticks
        feed._candles = []
        with pytest.raises(SourceError) as caught:
            source.fetch("NSE:INFY", 1, None)
        assert "Waiting for live ticks" in caught.value.message

    def test_kite_live_source_fetch_disabled_shows_feed_reason(self) -> None:
        class FakeFeedDisconnected:
            is_simulated = False

            def status(self):
                return FeedStatus(state=STATE_DISCONNECTED, reason="Server down")

            def bootstrap_data_for(self, _):
                return []

        source = KiteLiveSource(feed_provider=lambda: FakeFeedDisconnected())
        assert source.enabled is False
        with pytest.raises(SourceError) as caught:
            source.fetch("NSE:INFY", 1, None)
        assert "Server down" in caught.value.message


class TestNormaliseFrameAndDelay:
    def test_normalise_frame_empty_or_none(self) -> None:
        assert _normalise_frame(None).empty
        assert _normalise_frame(pd.DataFrame()).empty

    def test_normalise_frame_missing_columns(self) -> None:
        df = pd.DataFrame({"open": [1.0], "close": [2.0]})
        assert _normalise_frame(df).empty

    def test_normalise_frame_with_and_without_volume(self) -> None:
        df_vol = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [100]}
        )
        res = _normalise_frame(df_vol)
        assert not res.empty
        assert "volume" in res.columns

        df_no_vol = pd.DataFrame({"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5]})
        res2 = _normalise_frame(df_no_vol)
        assert not res2.empty
        assert res2["volume"].iloc[0] == 0.0

    def test_delay_seconds_calculation(self) -> None:
        assert _delay_seconds((), 1, 1000.0) is None
        c = Candle(time=1000, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)
        assert _delay_seconds((c,), 1, 1100.0) == 40.0


class TestWatchlist:
    def test_add_and_remove_persist_in_the_git_ignored_file(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        service = context.chart
        result = service.add_watchlist("nse_delayed", "NSE:INFY")
        added = next(item for item in result["items"] if item["symbol"] == "NSE:INFY")
        configured = next(item for item in result["items"] if item["symbol"] == "NSE:TEST")
        assert added["removable"] is True
        assert configured["removable"] is False
        path = tmp_path / "data" / "chart_watchlist.json"
        assert path.exists()
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["symbols"]["nse_delayed"] == ["NSE:INFY"]

        service.remove_watchlist("nse_delayed", "NSE:INFY")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["symbols"]["nse_delayed"] == []

    def test_invalid_or_fixed_sources_are_refused(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        service = context.chart
        with pytest.raises(ChartError):
            service.add_watchlist("crypto_showcase", "BTC")
        with pytest.raises(ChartError):
            service.add_watchlist("nse_delayed", "NFO:NIFTY26NOVFUT")
        with pytest.raises(ChartError):
            service.remove_watchlist("nse_delayed", "NSE:TEST")

    def test_configured_watchlist_symbols_are_used(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        result = context.chart.watchlist("nse_delayed")
        assert result["items"][0]["symbol"] == "NSE:TEST"
        assert result["items"][0]["sparkline"]


class TestClock:
    def test_open_preopen_weekend_and_holiday(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        service = context.chart
        monday_open = service.market_clock(dt.datetime(2026, 11, 16, 10, 30, tzinfo=IST))
        assert monday_open["state"] == "open"
        assert monday_open["is_open"] is True
        assert monday_open["countdown_seconds"] > 0
        assert monday_open["next_event"] == "Closes"

        early = service.market_clock(dt.datetime(2026, 11, 16, 8, 0, tzinfo=IST))
        assert early["state"] == "closed"
        assert early["next_event"] == "Opens"

        preopen = service.market_clock(dt.datetime(2026, 11, 16, 9, 5, tzinfo=IST))
        assert preopen["state"] == "pre-open"
        assert preopen["pre_open_phase"] == "entry"

        weekend = service.market_clock(dt.datetime(2026, 11, 15, 10, 30, tzinfo=IST))
        assert weekend["state"] == "weekend"
        assert weekend["is_open"] is False
        assert "weekend" in weekend["notice"].lower()

        assert context.settings is not None
        calendar = TradingCalendar(holidays=[dt.date(2026, 11, 16)])
        service._calendar_cache = (id(context.settings), calendar)
        holiday = service.market_clock(dt.datetime(2026, 11, 16, 10, 30, tzinfo=IST))
        assert holiday["state"] == "holiday"
        assert "holiday" in holiday["notice"].lower()


class TestChartService:
    def test_payload_has_freshness_indicators_and_signal(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path, clock_value="2026-11-16 14:20")
        payload = context.chart.candles("nse_delayed", "NSE:TEST", 5, 60)
        assert payload["badge"] == "DELAYED"
        assert len(payload["candles"]) == 60
        assert payload["candles"][-1]["ema_fast"] is not None
        assert payload["freshness"]["latest_time"] == "14:10"
        assert payload["freshness"]["age_text"] == "10 min old"
        assert "of 5 agree" in payload["signal"]["summary"]
        assert "No alerts are sent" in payload["signal"]["label"]
        assert payload["oi"]["available"] is False
        assert payload["oi"]["message"] == "No OI data from this source"

    def test_stale_failure_is_kept_visible(self, tmp_path: Path) -> None:
        context, fake = make_chart_context(tmp_path)
        fake.stale = True
        payload = context.chart.candles("nse_delayed", "NSE:TEST", 5, 30)
        assert payload["stale"] is True
        assert payload["freshness"]["stale"] is True
        assert payload["freshness"]["stale_since"]
        assert "unavailable" in payload["message"].lower()

    def test_invalid_interval_and_symbol_are_rejected(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        with pytest.raises(ChartError):
            context.chart.candles("nse_delayed", "NSE:TEST", 7, 30)
        with pytest.raises(ChartError):
            context.chart.candles("nse_delayed", "NFO:FUT", 5, 30)
        with pytest.raises(ChartError):
            context.chart.candles("made_up", "NSE:TEST", 5, 30)

    def test_kite_live_never_reaches_a_provider(self, tmp_path: Path) -> None:
        context, fake = make_chart_context(tmp_path)
        with pytest.raises(ChartError):
            context.chart.candles("kite_live", "NSE:TEST", 5, 30)
        assert fake.calls == []

    def test_chart_package_never_imports_alert_writing_code(self) -> None:
        import ast

        root = Path(__file__).resolve().parents[2] / "src" / "kite_alerts" / "ui" / "chart"
        imported: list[str] = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
        joined = "\n".join(imported)
        for name in ("engine", "notifiers", "persistence"):
            assert name not in joined
        assert "record_alert" not in joined
        assert "send_alert" not in joined
