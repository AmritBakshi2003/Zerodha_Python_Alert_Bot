"""Timezone handling.

The prototype used naive timestamps and ``datetime.now()``. On a UTC host (the
Docker default) that silently shifted every window by 5h30m, which breaks history
seeding and therefore every alert. Everything here is explicit about
``Asia/Kolkata`` (IST), and there is exactly one way to obtain "now".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

IST_ZONE = "Asia/Kolkata"
_FALLBACK = dt.timezone(dt.timedelta(hours=5, minutes=30), IST_ZONE)


def _load_ist() -> dt.tzinfo:
    """Prefer the real tz database, fall back to a fixed offset (India has no DST)."""
    try:
        return ZoneInfo(IST_ZONE)
    except (ZoneInfoNotFoundError, KeyError):  # pragma: no cover - needs a broken tzdata
        return _FALLBACK


IST: dt.tzinfo = _load_ist()

#: Values arrive from Kite as naive IST wall-clock datetimes.
NAIVE_ASSUMED_ZONE = IST


def to_ist(value: dt.datetime | pd.Timestamp | str) -> dt.datetime:
    """Return an IST-aware datetime. Naive input is *interpreted* as IST."""
    resolved: dt.datetime
    if isinstance(value, str):
        resolved = pd.Timestamp(value).floor("us").to_pydatetime()
    elif isinstance(value, pd.Timestamp):
        resolved = value.floor("us").to_pydatetime()
    else:
        resolved = value
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=IST)
    return resolved.astimezone(IST)


def to_ist_ts(value: dt.datetime | pd.Timestamp | str) -> pd.Timestamp:
    """Return an IST-aware ``pd.Timestamp`` (the canonical in-memory type)."""
    return cast(pd.Timestamp, pd.Timestamp(to_ist(value)))


def floor_minute(value: dt.datetime | pd.Timestamp) -> pd.Timestamp:
    """Floor to the start of the IST minute."""
    return cast(pd.Timestamp, to_ist_ts(value).floor("min"))


def ceil_minute(value: dt.datetime | pd.Timestamp) -> pd.Timestamp:
    ts = to_ist_ts(value)
    floored = cast(pd.Timestamp, ts.floor("min"))
    return floored if floored == ts else floored + pd.Timedelta(minutes=1)


def seconds_until_next_minute(value: dt.datetime | pd.Timestamp) -> float:
    """Seconds until the next minute boundary, clamped so loops cannot spin."""
    ts = to_ist_ts(value)
    delta = (cast(pd.Timestamp, ts.floor("min")) + pd.Timedelta(minutes=1) - ts).total_seconds()
    return max(float(delta), 0.05)


@runtime_checkable
class Clock(Protocol):
    """Injected everywhere time is read, so tests never depend on wall time."""

    def now(self) -> dt.datetime:
        """Current time as an IST-aware datetime."""
        ...


class SystemClock:
    """Wall clock in IST."""

    __slots__ = ()

    def now(self) -> dt.datetime:
        return dt.datetime.now(tz=IST)


@dataclass
class FrozenClock:
    """Deterministic clock for tests and replay."""

    current: dt.datetime

    def __post_init__(self) -> None:
        self.current = to_ist(self.current)

    def now(self) -> dt.datetime:
        return self.current

    def advance(self, seconds: float) -> dt.datetime:
        self.current = self.current + dt.timedelta(seconds=seconds)
        return self.current

    def set(self, value: dt.datetime | pd.Timestamp | str) -> dt.datetime:
        self.current = to_ist(value)
        return self.current


def format_hhmm(value: dt.datetime | pd.Timestamp) -> str:
    """``HH:MM`` in IST, regardless of host timezone."""
    return str(to_ist_ts(value).strftime("%H:%M"))
