"""NSE trading session and holiday calendar.

The guard is evaluated on **event time** (the candle/ tick timestamp), so replay
and live share one code path, and it never consults the host timezone.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .clock import IST, to_ist, to_ist_ts
from .errors import ConfigError

log = logging.getLogger(__name__)


def parse_hhmm(value: str) -> dt.time:
    """Parse ``HH:MM`` (or ``HH:MM:SS``) into a time, with a clear error."""
    try:
        return dt.time.fromisoformat(value.strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid time {value!r}; expected HH:MM (e.g. 09:15)") from exc


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """Inclusive trading window in IST."""

    open_time: dt.time = dt.time(9, 15)
    close_time: dt.time = dt.time(15, 30)

    def __post_init__(self) -> None:
        if self.open_time >= self.close_time:
            raise ConfigError(
                f"session open ({self.open_time}) must be before close ({self.close_time})"
            )

    @classmethod
    def parse(cls, open_value: str, close_value: str) -> SessionWindow:
        return cls(open_time=parse_hhmm(open_value), close_time=parse_hhmm(close_value))

    @property
    def minutes(self) -> int:
        start = dt.datetime.combine(dt.date(2000, 1, 1), self.open_time)
        end = dt.datetime.combine(dt.date(2000, 1, 1), self.close_time)
        return int((end - start).total_seconds() // 60)

    def contains(self, when: dt.datetime | pd.Timestamp) -> bool:
        moment = to_ist(when).time()
        return self.open_time <= moment <= self.close_time


class TradingCalendar:
    """Weekday + holiday aware session guard."""

    def __init__(
        self,
        window: SessionWindow | None = None,
        holidays: Iterable[dt.date] = (),
        *,
        source: str = "",
    ) -> None:
        self.window = window or SessionWindow()
        self._holidays = frozenset(holidays)
        self.source = source

    @property
    def holidays(self) -> frozenset[dt.date]:
        return self._holidays

    def is_weekend(self, day: dt.date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: dt.date) -> bool:
        return day in self._holidays

    def is_trading_day(self, when: dt.date | dt.datetime | pd.Timestamp) -> bool:
        day = (
            when
            if isinstance(when, dt.date) and not isinstance(when, dt.datetime)
            else to_ist(when).date()
        )
        return not self.is_weekend(day) and not self.is_holiday(day)

    def in_session(self, when: dt.datetime | pd.Timestamp) -> bool:
        moment = to_ist(when)
        return self.is_trading_day(moment.date()) and self.window.contains(moment)

    def session_bounds(
        self, when: dt.date | dt.datetime | pd.Timestamp
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        day = (
            when
            if isinstance(when, dt.date) and not isinstance(when, dt.datetime)
            else to_ist(when).date()
        )
        start = pd.Timestamp(dt.datetime.combine(day, self.window.open_time, IST))
        end = pd.Timestamp(dt.datetime.combine(day, self.window.close_time, IST))
        return start, end

    def session_minutes_elapsed(self, when: dt.datetime | pd.Timestamp) -> float:
        start, _ = self.session_bounds(when)
        return max(0.0, float((to_ist_ts(when) - start).total_seconds()) / 60.0)

    def describe(self, when: dt.datetime | pd.Timestamp) -> str:
        moment = to_ist(when)
        day = moment.date()
        if self.is_holiday(day):
            return "holiday"
        if self.is_weekend(day):
            return "weekend"
        if moment.time() < self.window.open_time:
            return "pre-open"
        if moment.time() > self.window.close_time:
            return "post-close"
        return "open"

    def next_open(self, when: dt.datetime | pd.Timestamp) -> pd.Timestamp:
        """Next session open strictly after ``when`` (max 14 days ahead)."""
        moment = to_ist_ts(when)
        for offset in range(0, 15):
            day = moment.date() + dt.timedelta(days=offset)
            if not self.is_trading_day(day):
                continue
            start, _ = self.session_bounds(day)
            if start > moment:
                return start
        return moment  # pragma: no cover - only with a fully holiday calendar

    def holidays_stale_days(self, today: dt.date) -> int | None:
        """Age (days) of the newest configured holiday, or None if the list is empty."""
        future = [h for h in self._holidays if h >= dt.date(today.year, 1, 1)]
        if not future:
            return None
        return (today - max(future)).days


def load_holidays(path: str | Path | None) -> frozenset[dt.date]:
    """Load NSE holidays.

    Accepted YAML shapes::

        holidays: [2026-01-26, 2026-03-04]
        holidays:
          2026-01-26: Republic Day

    An empty file is valid and is the safe default: fail-open to "trading day",
    with the staleness watchdog as the real protection against a closed market.
    """
    if not path:
        return frozenset()
    file_path = Path(path)
    if not file_path.exists():
        log.warning("holiday file %s not found; assuming no holidays", file_path)
        return frozenset()

    try:
        raw: Any = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"holiday file {file_path} is not valid YAML: {exc}") from exc

    if isinstance(raw, Mapping):
        entries = raw.get("holidays", [])
    else:
        entries = raw
    if entries is None:
        return frozenset()
    if isinstance(entries, Mapping):
        entries = list(entries.keys())
    if not isinstance(entries, list):
        raise ConfigError(
            f"holiday file {file_path}: 'holidays' must be a list of ISO dates or a mapping"
        )

    days: set[dt.date] = set()
    for entry in entries:
        try:
            days.add(pd.Timestamp(str(entry)).date())
        except (ValueError, TypeError) as exc:
            raise ConfigError(
                f"holiday file {file_path}: {entry!r} is not a date (use YYYY-MM-DD)"
            ) from exc
    return frozenset(days)


def calendar_from_config(
    *, open_value: str, close_value: str, holiday_file: str | Path | None
) -> TradingCalendar:
    window = SessionWindow.parse(open_value, close_value)
    holidays = load_holidays(holiday_file)
    return TradingCalendar(
        window=window,
        holidays=holidays,
        source=str(holiday_file) if holiday_file else "",
    )
