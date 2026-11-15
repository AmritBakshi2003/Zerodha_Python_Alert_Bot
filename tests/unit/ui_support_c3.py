"""Offline builders for the C3 chart tests.

No test here makes a network call: the adapters accept fake fetchers and the
FastAPI context is built with a fake source.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd

from kite_alerts.config.settings import Settings
from kite_alerts.core.clock import IST, FrozenClock
from kite_alerts.ui.chart.service import ChartService
from kite_alerts.ui.chart.sources import DemoSource, KiteLiveSource
from kite_alerts.ui.chart.transforms import from_frame, resample_candles
from kite_alerts.ui.chart.types import Candle, SourceError, SourceSeries, SymbolInfo
from unit.ui_support import make_client, make_context, make_settings


def make_frame(
    *,
    start: str = "2026-11-16 09:15",
    periods: int = 120,
    step: float = 0.1,
) -> pd.DataFrame:
    if periods <= 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
    index = pd.date_range(start, periods=periods, freq="min", tz=IST)
    close = [100.0 + index_i * step + (index_i % 7) * 0.03 for index_i in range(periods)]
    open_ = [close[0] - 0.1, *close[:-1]]
    return pd.DataFrame(
        {
            "open": open_,
            "high": [max(o, c) + 0.2 for o, c in zip(open_, close, strict=False)],
            "low": [min(o, c) - 0.2 for o, c in zip(open_, close, strict=False)],
            "close": close,
            "volume": [1000 + index_i for index_i in range(periods)],
            "oi": [1_000_000 + index_i * 1000 for index_i in range(periods)],
        },
        index=index,
    )


def make_candles(periods: int = 120) -> tuple[Candle, ...]:
    return from_frame(make_frame(periods=periods))


class FakeSource:
    id = "nse_delayed"
    label = "Delayed NSE"
    badge = "DELAYED"
    supports_oi = False
    enabled = True
    session_based = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.stale = False
        self.raise_error: SourceError | None = None
        self.candles = make_candles(periods=300)

    def symbols(self, settings: Settings | None) -> tuple[SymbolInfo, ...]:
        return (
            SymbolInfo("NSE:TEST", "TEST", "TEST.NS"),
            SymbolInfo(
                "NFO:FUT",
                "NFO:FUT",
                "",
                supported=False,
                reason="NFO futures and options are not available from the delayed NSE source.",
            ),
        )

    def fetch(self, symbol: str, interval: int, settings: Settings | None) -> SourceSeries:
        self.calls.append((symbol, interval))
        if self.raise_error is not None:
            raise self.raise_error
        candles = resample_candles(self.candles, interval) if interval != 1 else self.candles
        return SourceSeries(
            candles=candles,
            provider_interval=1,
            supports_oi=False,
            delay_seconds=120.0,
            stale=self.stale,
            stale_since=(
                int(dt.datetime(2026, 11, 16, 10, 0, tzinfo=IST).timestamp())
                if self.stale
                else None
            ),
            retry_after=30.0 if self.stale else 0.0,
            message="Delayed data is temporarily unavailable." if self.stale else "",
        )


class OfflineSource:
    """Metadata-only source for tests that never fetch it."""

    enabled = True
    supports_oi = False
    session_based = False

    def __init__(self, source_id: str, label: str, badge: str) -> None:
        self.id = source_id
        self.label = label
        self.badge = badge

    def symbols(self, settings: Settings | None) -> tuple[SymbolInfo, ...]:
        return ()

    def fetch(self, symbol: str, interval: int, settings: Settings | None) -> SourceSeries:
        raise SourceError("This test source is not available.")


def make_chart_context(
    tmp_path: Path,
    *,
    clock_value: str = "2026-11-16 10:30",
    fake: FakeSource | None = None,
) -> tuple[Any, FakeSource]:
    settings = make_settings(tmp_path / "alerts.db")
    context = make_context(settings, db_path=tmp_path / "alerts.db")
    chart = ChartService(
        settings_provider=lambda: context.settings,
        sources={
            "nse_delayed": fake or FakeSource(),
            "crypto_showcase": OfflineSource("crypto_showcase", "Crypto showcase", "SHOWCASE"),
            "demo": DemoSource(),
            "kite_live": KiteLiveSource(),
        },
        watchlist_path=tmp_path / "data" / "chart_watchlist.json",
        clock=FrozenClock(clock_value),
    )
    context.chart = chart
    return context, chart.sources["nse_delayed"]  # type: ignore[return-value]


def make_chart_client(context: Any):
    return make_client(context)


__all__ = [
    "FakeSource",
    "make_candles",
    "make_chart_client",
    "make_chart_context",
    "make_frame",
]
