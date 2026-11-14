"""Structural protocols used to keep layers independent and testable.

The engine depends on ``AlertStorePort``/``AlertSinkPort``, the data layer on
``MetricsPort``: none of them import a concrete implementation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

from .models import Alert, Candle, Message


@runtime_checkable
class MetricsPort(Protocol):
    def inc(self, name: str, value: float = 1.0, **labels: str) -> None: ...

    def set_gauge(self, name: str, value: float, **labels: str) -> None: ...

    def observe(self, name: str, value: float, **labels: str) -> None: ...


@runtime_checkable
class AlertStorePort(Protocol):
    def last_alert(self, symbol: str) -> tuple[str | None, pd.Timestamp | None]: ...

    def record_alert(self, alert: Alert) -> bool:
        """Persist an alert; returns False when it is a duplicate."""

    def mark_delivered(self, dedup_key: str, channels: list[str], ok: bool) -> None: ...

    def last_candle(self, symbol: str) -> pd.Timestamp | None: ...

    def set_last_candle(self, symbol: str, ts: pd.Timestamp) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class AlertSinkPort(Protocol):
    def submit(self, alert: Alert) -> None: ...


@runtime_checkable
class NotifierPort(Protocol):
    name: str

    def send(self, message: Message) -> None:
        """Deliver one message. Raises on failure (the hub owns retries)."""

    def close(self) -> None: ...


@runtime_checkable
class CandleSinkPort(Protocol):
    def on_candle(self, candle: Candle) -> Alert | None: ...


class NullMetrics:
    """Metrics implementation used when observability is disabled in tests."""

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        pass

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        pass

    def observe(self, name: str, value: float, **labels: str) -> None:
        pass


def metric(metrics: MetricsPort | None, name: str, value: float = 1.0, **labels: str) -> None:
    """Increment a metric if a metrics sink is present (never raises)."""
    if metrics is None:
        return
    try:
        metrics.inc(name, value, **labels)
    except Exception:  # observability must never break trading logic
        return


def gauge(metrics: MetricsPort | None, name: str, value: float, **labels: str) -> None:
    if metrics is None:
        return
    try:
        metrics.set_gauge(name, value, **labels)
    except Exception:
        return


__all__: list[str] = [
    "AlertSinkPort",
    "AlertStorePort",
    "CandleSinkPort",
    "MetricsPort",
    "NotifierPort",
    "NullMetrics",
    "gauge",
    "metric",
]
