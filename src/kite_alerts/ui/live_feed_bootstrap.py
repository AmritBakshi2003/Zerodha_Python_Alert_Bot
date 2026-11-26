"""History bootstrap helpers for the live feed (F12 / P5).

Pure helpers with no ``LiveFeedService`` dependency. Re-exported from
``live_feed.py`` so existing imports and monkeypatches keep working.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from ..core.clock import IST, to_ist_ts

log = logging.getLogger(__name__)


def _bootstrap_candles(
    kite: Any,
    token: int,
    symbol: str,
    *,
    days: int = 1,
) -> list[dict[str, Any]]:
    """Fetch recent 1-minute history via kite.historical_data.

    Returns a list of normalised dicts compatible with data.history.normalise_history.
    Returns [] on failure rather than crashing.
    """
    from ..data.history import normalise_history

    try:
        now_ist = dt.datetime.now(tz=IST)
        from_dt = (now_ist - dt.timedelta(days=days)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        to_dt = now_ist
        rows = kite.historical_data(
            token,
            from_dt.replace(tzinfo=None),  # Kite expects naive IST
            to_dt.replace(tzinfo=None),
            "minute",
            False,
            True,  # request OI; will be retried without if unsupported
        )
        frame = normalise_history(rows)
        if frame.empty:
            return []
        result: list[dict[str, Any]] = []
        for ts, row in frame.iterrows():
            epoch = int(to_ist_ts(ts).timestamp())
            result.append(
                {
                    "time": epoch,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row.get("volume", 0)),
                    "oi": float(row.get("oi", 0.0)),
                }
            )
        return result
    except Exception as exc:
        log.info("bootstrap failed for %s (%s)", symbol, type(exc).__name__)
        return []


def _bootstrap_without_oi(kite: Any, token: int, symbol: str) -> list[dict[str, Any]]:
    """Fallback bootstrap without OI for instruments that reject oi=True."""
    from ..data.history import normalise_history

    try:
        now_ist = dt.datetime.now(tz=IST)
        from_dt = (now_ist - dt.timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        to_dt = now_ist
        rows = kite.historical_data(
            token,
            from_dt.replace(tzinfo=None),
            to_dt.replace(tzinfo=None),
            "minute",
            False,
            False,
        )
        frame = normalise_history(rows)
        if frame.empty:
            return []
        result: list[dict[str, Any]] = []
        for ts, row in frame.iterrows():
            epoch = int(to_ist_ts(ts).timestamp())
            result.append(
                {
                    "time": epoch,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row.get("volume", 0)),
                    "oi": 0.0,
                }
            )
        return result
    except Exception as exc:
        log.info("bootstrap (no-OI) failed for %s (%s)", symbol, type(exc).__name__)
        return []
