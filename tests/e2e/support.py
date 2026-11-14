"""Helpers that assemble the *production* runtime with fake Kite objects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import SecretStr

from fakes.fake_kite import FakeInstrument, FakeKite, FakeTicker, RecordingNotifier
from kite_alerts.cli.run import Runtime, build_runtime
from kite_alerts.config.settings import (
    KiteCredentials,
    NotifySettings,
    ObservabilitySettings,
    ResilienceSettings,
    SessionSettings,
    Settings,
    StrategyParams,
)
from kite_alerts.core.clock import FrozenClock
from kite_alerts.demo import synthetic_day

TOKEN = 111
SYMBOL = "NSE:TEST"
SESSION_DAY = "2026-11-16"


def live_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "kite": KiteCredentials(
            api_key="key", api_secret=SecretStr("secret"), access_token=SecretStr("token")
        ),
        "symbols": (SYMBOL,),
        "params": StrategyParams(),
        "notify": NotifySettings(channels=("console",)),
        "session": SessionSettings(holiday_file=None),
        "resilience": ResilienceSettings(
            feed_stale_seconds=60,
            symbol_stale_seconds=3600,
            history_rate_limit_per_sec=50,
            history_max_retries=2,
            retry_base_seconds=0.01,
            tick_queue_size=50000,
        ),
        "observability": ObservabilitySettings(
            http_enabled=False, heartbeat_minutes=0, log_dir=tmp_path / "logs"
        ),
        "db_path": tmp_path / "alerts.db",
        "cache_dir": tmp_path / "cache",
        "warmup_days": 5,
    }
    base.update(overrides)
    return Settings(**base)


def make_fake_kite(warmup: pd.DataFrame, **kwargs: Any) -> FakeKite:
    return FakeKite(
        instruments=[FakeInstrument(TOKEN, "NSE", "TEST", name="TEST")],
        history={TOKEN: warmup},
        **kwargs,
    )


@dataclass
class Harness:
    runtime: Runtime
    fake: FakeKite
    clock: FrozenClock
    notifier: RecordingNotifier
    warmup: pd.DataFrame
    session: pd.DataFrame

    @property
    def ticker(self) -> FakeTicker:
        return FakeTicker.instances[-1]

    def start_feed(self) -> FakeTicker:
        self.runtime.hub.start()
        self.runtime.feed.start()
        return self.ticker

    def step(self, times: int = 1) -> None:
        for _ in range(times):
            self.runtime.worker.step(timeout=0)

    def at(self, minute: int) -> pd.Timestamp:
        """Timestamp of session bar ``minute`` (0 = 09:15)."""
        return self.session.index[minute]


def build_harness(
    tmp_path: Path, *, feed_stale_seconds: int = 600, **settings_overrides: Any
) -> Harness:
    FakeTicker.reset()
    data = synthetic_day()
    warmup, session = data.iloc[:50], data.iloc[50:]
    clock = FrozenClock(f"{SESSION_DAY} 09:00:00")
    fake = make_fake_kite(warmup)
    notifier = RecordingNotifier(name="recording")
    resilience = ResilienceSettings(
        feed_stale_seconds=feed_stale_seconds,
        symbol_stale_seconds=3600,
        history_rate_limit_per_sec=50,
        history_max_retries=2,
        retry_base_seconds=0.01,
        tick_queue_size=50000,
    )
    settings = live_settings(tmp_path, resilience=resilience, **settings_overrides)
    runtime = build_runtime(
        settings,
        kite=fake,
        ticker_factory=FakeTicker,
        clock=clock,
        notifiers=[notifier],
        start_health=False,
    )
    return Harness(
        runtime=runtime,
        fake=fake,
        clock=clock,
        notifier=notifier,
        warmup=warmup,
        session=session,
    )


def ticks_for_bar(
    ts: pd.Timestamp, row: pd.Series, *, cumulative_volume: int
) -> list[dict[str, Any]]:
    """Four ticks that reproduce one bar exactly (open, high, low, close)."""
    volumes = [
        cumulative_volume + 250,
        cumulative_volume + 500,
        cumulative_volume + 750,
        cumulative_volume + 1000,
    ]
    prices = [float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])]
    seconds = [1, 10, 20, 50]
    return [
        {
            "instrument_token": TOKEN,
            "last_price": price,
            "exchange_timestamp": (ts + pd.Timedelta(seconds=second))
            .tz_localize(None)
            .to_pydatetime(),
            "volume_traded": volume,
            "oi": float(row["oi"]),
        }
        for price, second, volume in zip(prices, seconds, volumes, strict=True)
    ]
