"""Shared fixtures: deterministic IST clock, calendars, settings and frames."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from kite_alerts.config.settings import (
    KiteCredentials,
    NotifySettings,
    ObservabilitySettings,
    ResilienceSettings,
    SessionSettings,
    Settings,
    StrategyParams,
)
from kite_alerts.core.calendar import SessionWindow, TradingCalendar
from kite_alerts.core.clock import FrozenClock
from kite_alerts.demo import synthetic_day
from kite_alerts.observability.metrics import Metrics
from kite_alerts.persistence.store import InMemoryAlertStore, SqliteAlertStore

#: A Monday inside the default session (no holiday configured).
SESSION_DAY = "2026-11-16"
PREVIOUS_DAY = "2026-11-13"


def ist(value: str) -> pd.Timestamp:
    return pd.Timestamp(value).tz_localize("Asia/Kolkata")


def make_frame(
    prices: Sequence[float],
    *,
    start: str = f"{SESSION_DAY} 09:15",
    oi: Sequence[float] | None = None,
    volume: int = 1000,
) -> pd.DataFrame:
    """One-minute OHLC frame from a close-price path."""
    index = pd.date_range(start, periods=len(prices), freq="min", tz="Asia/Kolkata")
    close = np.asarray(prices, dtype=float)
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 0.05,
            "low": np.minimum(open_, close) - 0.05,
            "close": close,
            "volume": volume,
            "oi": np.asarray(oi, dtype=float) if oi is not None else np.full(len(close), 1e6),
        },
        index=index,
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(f"{SESSION_DAY} 09:14:00")


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar()


@pytest.fixture
def window() -> SessionWindow:
    return SessionWindow()


@pytest.fixture
def params() -> StrategyParams:
    return StrategyParams()


@pytest.fixture
def metrics() -> Metrics:
    return Metrics()


@pytest.fixture
def memory_store() -> InMemoryAlertStore:
    return InMemoryAlertStore()


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SqliteAlertStore:
    store = SqliteAlertStore(tmp_path / "alerts.db")
    yield store
    store.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        kite=KiteCredentials(api_key="test_key"),
        symbols=("NSE:TEST",),
        params=StrategyParams(),
        notify=NotifySettings(channels=("console",)),
        session=SessionSettings(holiday_file=None),
        resilience=ResilienceSettings(feed_stale_seconds=60, symbol_stale_seconds=120),
        observability=ObservabilitySettings(http_enabled=False, log_dir=tmp_path / "logs"),
        db_path=tmp_path / "kite_alerts.db",
        cache_dir=tmp_path / "cache",
        warmup_days=2,
    )


@pytest.fixture
def demo_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(warm-up rows, session rows) from the deterministic synthetic day."""
    data = synthetic_day()
    return data.iloc[:50], data.iloc[50:]


@pytest.fixture
def trending_up_frame() -> pd.DataFrame:
    """A rally strong enough to make every rule bullish at the last bar."""
    prices = np.r_[np.linspace(100, 99, 40), np.linspace(99, 106, 25)]
    return make_frame(prices, start=f"{SESSION_DAY} 09:15")


def make_holidays(path: Path, days: Iterable[str]) -> Path:
    path.write_text("holidays:\n" + "".join(f"  - {day}\n" for day in days), encoding="utf-8")
    return path


def write_env(path: Path, values: dict[str, Any]) -> Path:
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def monday() -> dt.date:
    return dt.date(2026, 11, 16)
