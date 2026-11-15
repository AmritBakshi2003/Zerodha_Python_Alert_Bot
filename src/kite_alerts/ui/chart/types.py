"""Core value types for the read-only chart workstation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Candle:
    """One normalised candle. ``time`` is a true UTC epoch second."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: float | None = None

    def as_dict(self) -> dict[str, Any]:
        from .transforms import ist_label

        return {
            "time": self.time,
            "ist": ist_label(self.time),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "oi": self.oi,
        }


@dataclass(frozen=True, slots=True)
class RawSeries:
    """Provider output before indicators and trimming."""

    candles: tuple[Candle, ...]
    provider_interval: int
    supports_oi: bool = False
    delay_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SourceSeries:
    """Provider output plus cache/staleness metadata."""

    candles: tuple[Candle, ...]
    provider_interval: int
    supports_oi: bool = False
    delay_seconds: float | None = None
    stale: bool = False
    stale_since: int | None = None
    retry_after: float = 0.0
    message: str = ""


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    symbol: str
    label: str
    provider_symbol: str = ""
    supported: bool = True
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "label": self.label,
            "provider_symbol": self.provider_symbol,
            "supported": self.supported,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SourceInfo:
    id: str
    label: str
    badge: str
    available: bool = True
    enabled: bool = True
    reason: str = ""
    notice: str = ""
    supports_oi: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "badge": self.badge,
            "available": self.available,
            "enabled": self.enabled,
            "reason": self.reason,
            "notice": self.notice,
            "supports_oi": self.supports_oi,
        }


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """What a service call returns: payload plus HTTP-ish status."""

    status: int
    payload: dict[str, Any] = field(default_factory=dict)


class SourceError(Exception):
    """A user-facing provider failure that never carries a URL or response body."""

    def __init__(self, message: str, *, retry_after: float = 0.0, code: str = "unavailable"):
        self.message = message
        self.retry_after = retry_after
        self.code = code
        super().__init__(message)


class ChartError(Exception):
    """Validation or orchestration failure for a chart route."""

    def __init__(self, message: str, *, status: int = 422, code: str = "invalid"):
        self.message = message
        self.status = status
        self.code = code
        super().__init__(message)
