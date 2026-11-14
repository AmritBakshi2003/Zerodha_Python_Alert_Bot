"""Chart series for the demo player, computed with the bot's own indicator code.

Nothing here re-implements a signal: the EMA, RSI, MACD and opening-range values
come from :mod:`kite_alerts.indicators`, with the same parameters the engine
used. The browser only draws them.

Lightweight Charts expects a UNIX timestamp (seconds). Because our data is IST
wall-clock time and the chart renders timestamps in UTC, the value sent is the
IST wall clock expressed "as if" it were UTC, so the axis reads 09:15 rather than
03:45. No timezone maths happens in the browser.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, cast

import pandas as pd

from ..config.settings import StrategyParams
from ..core.clock import to_ist_ts
from ..indicators import ema, macd, opening_range, rsi


def chart_time(value: dt.datetime | pd.Timestamp | str) -> int:
    """IST wall-clock time as a UTC-like epoch second (chart display convention)."""
    naive = to_ist_ts(value).tz_localize(None)
    return int(naive.value // 1_000_000_000)


def number(value: Any) -> float | None:
    """JSON-safe float: NaN/inf become ``None`` (the browser's "no value")."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True, slots=True)
class ChartSeries:
    session_date: str
    candles: tuple[dict[str, Any], ...]
    orb: dict[str, Any] | None


def build_chart(
    frame: pd.DataFrame,
    params: StrategyParams,
    *,
    session_open: dt.time = dt.time(9, 15),
) -> ChartSeries:
    """Candles plus indicator series for the last session present in ``frame``."""
    if frame.empty:
        return ChartSeries(session_date="", candles=(), orb=None)

    last_ts = to_ist_ts(frame.index[-1])
    session_date = last_ts.date()
    session = frame[frame.index.date == session_date]

    fast = ema(frame["close"], params.ema_fast, seed=params.indicator_seed)
    slow = ema(frame["close"], params.ema_slow, seed=params.indicator_seed)
    rsi_line = rsi(frame["close"], params.rsi_period, seed=params.indicator_seed)
    macd_line, signal_line, histogram = macd(
        frame["close"],
        params.macd_fast,
        params.macd_slow,
        params.macd_signal,
        seed=params.indicator_seed,
    )

    candles: list[dict[str, Any]] = []
    for ts, row in session.iterrows():
        stamp = to_ist_ts(cast(Any, ts))
        candles.append(
            {
                "time": chart_time(stamp),
                "open": number(row["open"]),
                "high": number(row["high"]),
                "low": number(row["low"]),
                "close": number(row["close"]),
                "ema_fast": number(fast.loc[stamp]),
                "ema_slow": number(slow.loc[stamp]),
                "rsi": number(rsi_line.loc[stamp]),
                "macd": number(macd_line.loc[stamp]),
                "macd_signal": number(signal_line.loc[stamp]),
                "macd_hist": number(histogram.loc[stamp]),
            }
        )

    opening = opening_range(frame, params.orb_minutes, session_open=session_open)
    orb: dict[str, Any] | None = None
    if opening is not None:
        orb = {
            "high": opening.high,
            "low": opening.low,
            "start": chart_time(opening.start),
            "formed_at": chart_time(opening.end),
            "bars": opening.bars,
            "label": f"Opening range {opening.start.strftime('%H:%M')}–"
            f"{opening.end.strftime('%H:%M')}",
        }
    return ChartSeries(session_date=session_date.isoformat(), candles=tuple(candles), orb=orb)
