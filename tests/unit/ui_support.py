"""Builders shared by the dashboard tests.

Everything here is offline: no notifier touches a network, the status probe is
injected, and the only database is a temporary SQLite file.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi.testclient import TestClient

from kite_alerts.config.settings import (
    Channel,
    KiteCredentials,
    NotifySettings,
    ObservabilitySettings,
    SessionSettings,
    Settings,
    StrategyParams,
)
from kite_alerts.core.models import Alert, Message, Side
from kite_alerts.persistence.store import SqliteAlertStore
from kite_alerts.ui.app import UiContext, create_app
from kite_alerts.ui.history import AlertHistoryReader
from kite_alerts.ui.status import StatusService
from kite_alerts.ui.test_alert import TestAlertService

BASE_URL = "http://127.0.0.1:8765"
TODAY = "2026-11-16"
REPO_ROOT = Path(__file__).resolve().parents[2]


class FixedClock:
    """A ``Clock`` implementation pinned to one IST moment."""

    def __init__(self, value: str = f"{TODAY} 15:00") -> None:
        self._value = pd.Timestamp(value).tz_localize("Asia/Kolkata").to_pydatetime()

    def now(self) -> dt.datetime:
        return self._value


class FakeMonotonic:
    """Manually advanced monotonic counter for the rate-limit test."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeNotifier:
    """Records sends and optionally fails, like a broken channel would."""

    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name = name
        self.error = error
        self.sent = 0
        self.closed = False

    def send(self, message: Message) -> None:
        self.sent += 1
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


class FakeNotifierFactory:
    """Factory matching ``build_notifiers``'s call shape."""

    def __init__(self, specs: list[tuple[str, Exception | None]]) -> None:
        self.specs = specs
        self.created: list[FakeNotifier] = []

    def __call__(self, _settings: NotifySettings) -> list[FakeNotifier]:
        self.created = [FakeNotifier(name, error) for name, error in self.specs]
        return list(self.created)


def make_settings(
    db_path: Path,
    *,
    channels: tuple[str, ...] = ("console",),
    http_enabled: bool = True,
) -> Settings:
    telegrams = Channel.TELEGRAM in channels or any(name == "telegram" for name in channels)
    return Settings(
        kite=KiteCredentials(
            api_key="test_key",
            api_secret="dummy-secret",
            access_token="dummy-token",
        ),
        symbols=("NSE:TEST",),
        params=StrategyParams(),
        notify=NotifySettings(
            channels=tuple(Channel(name) for name in channels),
            telegram_bot_token="123456:FAKE-TELEGRAM-TOKEN-VALUE" if telegrams else "",
            telegram_chat_id="4242" if telegrams else "",
        ),
        session=SessionSettings(holiday_file=None),
        observability=ObservabilitySettings(http_enabled=http_enabled, http_port=9109),
        db_path=db_path,
    )


def make_alert(
    *,
    symbol: str = "NSE:TEST",
    side: Side = Side.BUY,
    when: str = f"{TODAY} 10:00",
    price: float = 100.0,
    reasons: tuple[str, ...] = ("RSI 61.0 in bullish zone",),
) -> Alert:
    return Alert(
        symbol=symbol,
        side=side,
        ts=pd.Timestamp(when).tz_localize("Asia/Kolkata"),
        price=price,
        reasons=reasons,
        rules_used=("rsi_zone",),
        rules_available=4,
        params={"ema_fast": 9},
    )


def seed_alerts(path: Path, alerts: list[Alert]) -> None:
    store = SqliteAlertStore(path)
    try:
        for alert in alerts:
            store.record_alert(alert)
    finally:
        store.close()


def make_context(
    settings: Settings | None,
    *,
    db_path: Path | None = None,
    probe: Callable[[str, float], tuple[int, dict[str, Any]] | None] | None = None,
    notifier_factory: Any | None = None,
    settings_error: str | None = None,
    history_clock: Any | None = None,
    test_clock: Callable[[], float] | None = None,
) -> UiContext:
    path = db_path if db_path is not None else (settings.db_path if settings else None)
    assert path is not None
    clock = history_clock if history_clock is not None else _default_clock()
    history = AlertHistoryReader(path, clock=clock)
    status = StatusService(
        settings,
        probe=probe if probe is not None else (lambda _url, _timeout: None),
        clock=clock,
    )
    kwargs: dict[str, Any] = {"settings_error": settings_error}
    if notifier_factory is not None:
        kwargs["notifier_factory"] = notifier_factory
    if test_clock is not None:
        kwargs["clock"] = test_clock
    return UiContext(
        settings=settings,
        settings_error=settings_error,
        history=history,
        status=status,
        test_alerts=TestAlertService(settings, **kwargs),
    )


def _default_clock() -> FixedClock:
    return FixedClock()


def make_client(context: UiContext, *, base_url: str = BASE_URL) -> TestClient:
    return TestClient(create_app(context), base_url=base_url)


__all__ = [
    "BASE_URL",
    "REPO_ROOT",
    "TODAY",
    "FakeMonotonic",
    "FakeNotifier",
    "FakeNotifierFactory",
    "FixedClock",
    "make_alert",
    "make_client",
    "make_context",
    "make_settings",
    "seed_alerts",
]
