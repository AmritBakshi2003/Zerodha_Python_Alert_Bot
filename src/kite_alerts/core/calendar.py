"""NSE trading session and holiday calendar.

The guard is evaluated on **event time** (the candle/ tick timestamp), so replay
and live share one code path, and it never consults the host timezone.

P1 (F3 + F4): ``session_info()`` is the single source of truth for what phase the
market is in, and every surface (health, dashboard clock, logs) renders from it.
The tradable window is unchanged: 09:15-15:30 inclusive, weekends and configured
holidays excepted, plus explicitly configured special sessions.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .clock import IST, SystemClock, to_ist, to_ist_ts
from .errors import ConfigError

log = logging.getLogger(__name__)

# Session states. These strings are stable: the dashboard, the bot health
# payload and the tests all compare against them.
STATE_CLOSED = "closed"
STATE_PRE_OPEN = "pre-open"
STATE_OPEN = "open"
STATE_CLOSING = "closing"
STATE_WEEKEND = "weekend"
STATE_HOLIDAY = "holiday"
STATE_SPECIAL = "special-session"

SESSION_STATES = frozenset(
    {
        STATE_CLOSED,
        STATE_PRE_OPEN,
        STATE_OPEN,
        STATE_CLOSING,
        STATE_WEEKEND,
        STATE_HOLIDAY,
        STATE_SPECIAL,
    }
)

#: NSE pre-open sub-phases (nseindia.com: entry 09:00-09:10, matching 09:10-09:12,
#: buffer 09:12-09:15).
PRE_OPEN_PHASES = ("entry", "matching", "buffer")


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


@dataclass(frozen=True, slots=True)
class SpecialSession:
    """A dated exception to the weekday window (muhurat, special Saturday...)."""

    day: dt.date
    open_time: dt.time
    close_time: dt.time
    label: str = "Special session"

    def __post_init__(self) -> None:
        if self.open_time >= self.close_time:
            raise ConfigError(
                f"special session on {self.day}: open ({self.open_time}) must be before "
                f"close ({self.close_time})"
            )

    @property
    def window(self) -> SessionWindow:
        return SessionWindow(open_time=self.open_time, close_time=self.close_time)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Everything a surface may say about "what is the market doing right now?"."""

    state: str
    label: str
    detail: str
    tradable: bool
    opens_at: str = ""
    closes_at: str = ""
    pre_open_phase: str | None = None
    is_special: bool = False
    session_open: str = "09:15"
    session_close: str = "15:30"
    now_ist: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "label": self.label,
            "detail": self.detail,
            "tradable": self.tradable,
            "is_open": self.tradable,
            "opens_at": self.opens_at,
            "closes_at": self.closes_at,
            "pre_open_phase": self.pre_open_phase,
            "is_special": self.is_special,
            "session_open": self.session_open,
            "session_close": self.session_close,
            "now_ist": self.now_ist,
        }


@dataclass
class TradingCalendar:
    """Weekday + holiday + special-session aware session guard."""

    window: SessionWindow = field(default_factory=SessionWindow)
    holidays: Iterable[dt.date] = ()
    special_sessions: Sequence[SpecialSession] = ()
    pre_open_time: dt.time = dt.time(9, 0)
    closing_end: dt.time = dt.time(16, 0)
    source: str = ""
    _holidays: frozenset[dt.date] = field(default=frozenset(), init=False, repr=False)
    _special: dict[dt.date, SpecialSession] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._holidays = frozenset(self.holidays)
        self._special = {session.day: session for session in self.special_sessions}
        if self.pre_open_time >= self.window.open_time:
            raise ConfigError(
                f"pre-open start ({self.pre_open_time}) must be before session open "
                f"({self.window.open_time})"
            )
        if self.closing_end <= self.window.close_time:
            raise ConfigError(
                f"closing end ({self.closing_end}) must be after session close "
                f"({self.window.close_time})"
            )

    # ------------------------------------------------------------------ basics
    @property
    def holiday_dates(self) -> frozenset[dt.date]:
        return self._holidays

    @property
    def configured_special_sessions(self) -> tuple[SpecialSession, ...]:
        return tuple(self._special.values())

    def special_session(self, day: dt.date) -> SpecialSession | None:
        return self._special.get(day)

    def is_weekend(self, day: dt.date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: dt.date) -> bool:
        return day in self._holidays

    def window_for(self, day: dt.date) -> SessionWindow:
        special = self._special.get(day)
        return special.window if special is not None else self.window

    def is_trading_day(self, when: dt.date | dt.datetime | pd.Timestamp) -> bool:
        day = self._day(when)
        # A configured special session is tradable even on a weekend/holiday.
        if day in self._special:
            return True
        return not self.is_weekend(day) and not self.is_holiday(day)

    def in_session(self, when: dt.datetime | pd.Timestamp) -> bool:
        return self.session_info(when).tradable

    def session_bounds(
        self, when: dt.date | dt.datetime | pd.Timestamp
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        day = self._day(when)
        window = self.window_for(day)
        start = pd.Timestamp(dt.datetime.combine(day, window.open_time, IST))
        end = pd.Timestamp(dt.datetime.combine(day, window.close_time, IST))
        return start, end

    def session_minutes_elapsed(self, when: dt.datetime | pd.Timestamp) -> float:
        start, _ = self.session_bounds(when)
        return max(0.0, float((to_ist_ts(when) - start).total_seconds()) / 60.0)

    # ------------------------------------------------------------------ states
    def session_info(self, when: dt.datetime | pd.Timestamp) -> SessionInfo:
        """The one description of the market phase at ``when`` (F4)."""
        moment = to_ist(when)
        day = moment.date()
        clock = moment.time()
        special = self._special.get(day)
        window = special.window if special is not None else self.window
        base: dict[str, Any] = {
            "session_open": window.open_time.strftime("%H:%M"),
            "session_close": window.close_time.strftime("%H:%M"),
            "now_ist": moment.strftime("%H:%M:%S"),
        }

        if special is not None and window.contains(moment):
            return SessionInfo(
                state=STATE_SPECIAL,
                label=special.label,
                detail=(f"Special session {window.open_time:%H:%M}-{window.close_time:%H:%M} IST."),
                tradable=True,
                opens_at=self._iso(day, window.open_time),
                closes_at=self._iso(day, window.close_time),
                is_special=True,
                **base,
            )
        if special is not None and clock > window.close_time:
            return self._closed_info(
                STATE_CLOSED,
                label=f"Closed — {special.label} finished",
                detail=(
                    f"The special session ended at {window.close_time:%H:%M} IST; "
                    "there is no regular session today."
                ),
                moment=moment,
                window=window,
                base=base,
            )
        if self.is_holiday(day):
            return self._closed_info(
                STATE_HOLIDAY,
                label="Closed — holiday",
                detail="NSE is closed today for a configured holiday.",
                moment=moment,
                window=window,
                base=base,
            )
        if self.is_weekend(day):
            return self._closed_info(
                STATE_WEEKEND,
                label="Closed — weekend",
                detail="NSE is closed for the weekend.",
                moment=moment,
                window=window,
                base=base,
            )
        if clock < self.pre_open_time:
            return self._closed_info(
                STATE_CLOSED,
                label="Closed",
                detail="Market has not opened yet.",
                moment=moment,
                window=window,
                base=base,
            )
        if clock < window.open_time:
            phase = self._pre_open_phase(clock)
            return SessionInfo(
                state=STATE_PRE_OPEN,
                label=f"Pre-open ({phase})",
                detail=self._pre_open_detail(phase, window),
                tradable=False,
                opens_at=self._iso(day, window.open_time),
                pre_open_phase=phase,
                **base,
            )
        if clock <= window.close_time:
            return SessionInfo(
                state=STATE_OPEN,
                label="Open",
                detail=(
                    f"Trading now. Session {window.open_time:%H:%M}-{window.close_time:%H:%M} IST."
                ),
                tradable=True,
                opens_at=self._iso(day, window.open_time),
                closes_at=self._iso(day, window.close_time),
                **base,
            )
        if clock <= self.closing_end:
            return SessionInfo(
                state=STATE_CLOSING,
                label="Closing",
                detail=f"Session ended at {window.close_time:%H:%M}; closing window until "
                f"{self.closing_end:%H:%M} IST.",
                tradable=False,
                opens_at=self._iso(day, window.open_time),
                closes_at=self._iso(day, window.close_time),
                **base,
            )
        return self._closed_info(
            STATE_CLOSED,
            label="Closed",
            detail=f"Session finished at {window.close_time:%H:%M}.",
            moment=moment,
            window=window,
            base=base,
        )

    def describe(self, when: dt.datetime | pd.Timestamp) -> str:
        """Backwards-compatible state string; the UI should use ``session_info``."""
        return self.session_info(when).state

    # --------------------------------------------------------------- navigation
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
        """Age (days) of the newest configured holiday in ``today``'s year, or None."""
        current = [h for h in self._holidays if h.year == today.year]
        if not current:
            return None
        return abs((today - max(current)).days)

    def holiday_warnings(self, today: dt.date) -> list[str]:
        """At most one actionable warning about the holiday list (F3)."""
        where = self.source or "the holiday file"
        if not self._holidays:
            return [
                f"{where} has no holidays for {today.year}; a market holiday will look like "
                "a stalled feed. Run scripts/refresh_holidays.py to load the official NSE list."
            ]
        if not any(holiday.year >= today.year for holiday in self._holidays):
            newest = max(self._holidays)
            return [
                f"{where} has no holidays for {today.year} (newest is {newest.isoformat()}); "
                f"refresh it with scripts/refresh_holidays.py or "
                f"{today.year} holidays will not be detected."
            ]
        return []

    # ---------------------------------------------------------------- internals
    @staticmethod
    def _iso(day: dt.date, when: dt.time) -> str:
        # IST-anchored: the UI subtracts these from "now" to count down.
        return dt.datetime.combine(day, when, IST).isoformat(timespec="minutes")

    @staticmethod
    def _day(when: dt.date | dt.datetime | pd.Timestamp) -> dt.date:
        if isinstance(when, dt.date) and not isinstance(when, dt.datetime):
            return when
        return to_ist(when).date()

    def _pre_open_phase(self, clock: dt.time) -> str:
        start = dt.datetime.combine(dt.date(2000, 1, 1), self.pre_open_time)
        now = dt.datetime.combine(dt.date(2000, 1, 1), clock)
        elapsed = (now - start).total_seconds() / 60.0
        if elapsed < 10:
            return "entry"
        if elapsed < 12:
            return "matching"
        return "buffer"

    def _pre_open_detail(self, phase: str, window: SessionWindow) -> str:
        spans = {
            "entry": (self.pre_open_time, dt.time(9, 10), "order entry"),
            "matching": (dt.time(9, 10), dt.time(9, 12), "order matching"),
            "buffer": (dt.time(9, 12), window.open_time, "buffer before the open"),
        }
        start, end, what = spans[phase]
        return f"Pre-open {what} {start:%H:%M}-{end:%H:%M} IST."

    def _closed_info(
        self,
        state: str,
        *,
        label: str,
        detail: str,
        moment: dt.datetime,
        window: SessionWindow,
        base: Mapping[str, Any],
    ) -> SessionInfo:
        opens = self.next_open(moment)
        return SessionInfo(
            state=state,
            label=label,
            detail=f"{detail} Next open {opens:%a %d %b %H:%M} IST.",
            tradable=False,
            opens_at=opens.isoformat(timespec="minutes"),
            closes_at=self._iso(moment.date(), window.close_time),
            **base,
        )


def load_holidays(path: str | Path | None) -> frozenset[dt.date]:
    """Load NSE holidays.

    Accepted YAML shapes::

        holidays: [2026-01-26, 2026-03-04]
        holidays:
          2026-01-26: Republic Day

    An empty file is valid and is the safe default: fail-open to "trading day",
    with ``holiday_warnings`` and the staleness watchdog as the real protection
    against a closed market.
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


def load_special_sessions(path: str | Path | None) -> tuple[SpecialSession, ...]:
    """Load dated session overrides (muhurat, special Saturday...).

    Shape::

        special_sessions:
          - date: 2026-11-08
            open: "18:15"
            close: "19:15"
            label: Muhurat trading
    """
    if not path:
        return ()
    file_path = Path(path)
    if not file_path.exists():
        return ()
    try:
        raw: Any = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"session file {file_path} is not valid YAML: {exc}") from exc

    entries = raw.get("special_sessions", []) if isinstance(raw, Mapping) else raw
    if not entries:
        return ()
    if not isinstance(entries, list):
        raise ConfigError(f"session file {file_path}: 'special_sessions' must be a list")

    sessions: list[SpecialSession] = []
    seen: set[dt.date] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ConfigError(f"session file {file_path}: each entry must be a mapping")
        try:
            day = pd.Timestamp(str(entry["date"])).date()
            open_time = parse_hhmm(str(entry["open"]))
            close_time = parse_hhmm(str(entry["close"]))
        except KeyError as exc:
            raise ConfigError(
                f"session file {file_path}: entry {entry!r} needs date, open and close"
            ) from exc
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"session file {file_path}: {entry!r}: {exc}") from exc
        if day in seen:
            raise ConfigError(f"session file {file_path}: duplicate special session on {day}")
        seen.add(day)
        sessions.append(
            SpecialSession(
                day=day,
                open_time=open_time,
                close_time=close_time,
                label=str(entry.get("label") or "Special session"),
            )
        )
    return tuple(sorted(sessions, key=lambda session: session.day))


def calendar_from_config(
    *,
    open_value: str,
    close_value: str,
    holiday_file: str | Path | None,
    special_sessions_file: str | Path | None = None,
    pre_open_value: str = "09:00",
    closing_value: str = "16:00",
    today: dt.date | None = None,
    clock: Any | None = None,
) -> TradingCalendar:
    """Build the calendar and emit the one F3 warning when the list is unusable."""
    calendar = TradingCalendar(
        window=SessionWindow.parse(open_value, close_value),
        holidays=load_holidays(holiday_file),
        special_sessions=load_special_sessions(special_sessions_file),
        pre_open_time=parse_hhmm(pre_open_value),
        closing_end=parse_hhmm(closing_value),
        source=str(holiday_file) if holiday_file else "",
    )
    day = today or to_ist((clock or SystemClock()).now()).date()
    for message in calendar.holiday_warnings(day):
        log.warning("%s", message)
    return calendar
