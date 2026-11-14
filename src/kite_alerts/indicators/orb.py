"""Opening range (first N minutes of the IST session).

The prototype derived "today" from ``df.index[-1].date()`` on naive timestamps,
which is only correct on an IST host. This version is explicit about the session
open and works on tz-aware IST frames.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from ..core.clock import IST, to_ist_ts


@dataclass(frozen=True, slots=True)
class OpeningRange:
    high: float
    low: float
    start: pd.Timestamp
    end: pd.Timestamp
    bars: int


def opening_range(
    frame: pd.DataFrame,
    minutes: int,
    *,
    session_open: dt.time = dt.time(9, 15),
) -> OpeningRange | None:
    """High/low of the first ``minutes`` after ``session_open`` for the last bar's day.

    Returns ``None`` when the range is incomplete (before ``start + minutes``) or
    when no bars for the window are present (e.g. a mid-session restart with a
    short history).
    """
    if minutes < 1:
        raise ValueError(f"minutes must be >= 1, got {minutes}")
    if frame.empty:
        return None

    last = to_ist_ts(frame.index[-1])
    day = last.date()
    start = pd.Timestamp(dt.datetime.combine(day, session_open, IST))
    end = start + pd.Timedelta(minutes=minutes)

    if last < end:
        return None
    window = frame[(frame.index >= start) & (frame.index < end)]
    if window.empty:
        return None
    return OpeningRange(
        high=float(window["high"].max()),
        low=float(window["low"].min()),
        start=start,
        end=end,
        bars=len(window),
    )
