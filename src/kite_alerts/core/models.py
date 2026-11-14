"""Immutable domain models shared by every layer.

``Alert.message()`` reproduces the prototype's alert text byte-for-byte; the
demo's golden output and the client-facing format depend on it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

import pandas as pd

from .clock import format_hhmm, to_ist_ts


class Side(str, Enum):
    """Direction of an *alert* (never an order)."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def icon(self) -> str:
        return "🟢" if self is Side.BUY else "🔴"


@dataclass(frozen=True, slots=True)
class Tick:
    """A single normalised market-data tick."""

    symbol: str
    token: int
    ts: pd.Timestamp
    price: float
    volume_cum: int | None = None
    oi: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", to_ist_ts(self.ts))


@dataclass(frozen=True, slots=True)
class Candle:
    """A closed one-minute candle."""

    symbol: str
    time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    oi: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "time", to_ist_ts(self.time))

    def as_dict(self) -> dict[str, Any]:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "oi": self.oi,
        }

    def to_row(self) -> pd.DataFrame:
        """Single-row frame indexed by ``time`` (matches the prototype merge path)."""
        return cast(pd.DataFrame, pd.DataFrame([self.as_dict()]).set_index("time"))


@dataclass(frozen=True, slots=True)
class RuleVote:
    """One rule's contribution to the vote."""

    rule_id: str
    vote: int
    reason: str = ""
    weight: float = 1.0

    @property
    def available(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Alert:
    """A fired alert, including the evidence needed to audit it later."""

    symbol: str
    side: Side
    ts: pd.Timestamp
    price: float
    reasons: tuple[str, ...] = ()
    rules_available: int = 0
    params: Mapping[str, Any] = field(default_factory=dict)
    rules_used: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", to_ist_ts(self.ts))
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def dedup_key(self) -> str:
        return f"{self.symbol}|{self.side.value}|{self.ts.isoformat()}"

    def message(self) -> str:
        """Exactly the prototype's alert line."""
        return (
            f"{self.side.icon} {self.side.value} {self.symbol} @ {self.price:.2f} "
            f"[{format_hhmm(self.ts)}] — " + "; ".join(self.reasons)
        )

    def audit_record(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "symbol": self.symbol,
            "side": self.side.value,
            "price": round(float(self.price), 4),
            "reasons": list(self.reasons),
            "rules_used": list(self.rules_used),
            "rules_available": self.rules_available,
            "params": dict(self.params),
        }


@dataclass(frozen=True, slots=True)
class Message:
    """A notification payload, independent of transport."""

    text: str
    level: str = "alert"  # alert | info | system | error
    subject: str = ""
    dedup_key: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def with_subject(self, subject: str) -> Message:
        return Message(
            text=self.text,
            level=self.level,
            subject=subject,
            dedup_key=self.dedup_key,
            meta=self.meta,
        )


def coerce_sequence(value: Sequence[Any] | None) -> tuple[Any, ...]:
    return tuple(value) if value else ()
