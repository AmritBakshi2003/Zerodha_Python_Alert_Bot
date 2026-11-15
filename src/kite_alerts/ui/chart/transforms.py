"""Pure candle transforms: timestamps, resampling and Heikin-Ashi.

These functions are deliberately independent of providers and of the web layer,
so their known-value tests do not need a network or a clock.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from ...core.clock import IST, to_ist_ts
from .types import Candle


def ist_label(epoch: int) -> str:
    """``YYYY-MM-DD HH:MM`` in IST for a true UTC epoch second."""
    return dt.datetime.fromtimestamp(int(epoch), tz=IST).strftime("%Y-%m-%d %H:%M")


def ist_time_label(epoch: int) -> str:
    return dt.datetime.fromtimestamp(int(epoch), tz=IST).strftime("%H:%M")


def age_text(seconds: float | None) -> str:
    if seconds is None:
        return "unknown age"
    if seconds < 60:
        return "less than a minute old"
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes} min old"
    hours = int(seconds // 3600)
    if hours < 48:
        return f"{hours} h old"
    return f"{int(seconds // 86400)} d old"


def to_frame(candles: tuple[Candle, ...] | list[Candle]) -> pd.DataFrame:
    """Build an IST-indexed OHLCV frame from normalised candles."""
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
    index = pd.DatetimeIndex(
        [pd.Timestamp(candle.time, unit="s", tz="UTC").tz_convert(IST) for candle in candles]
    )
    data: dict[str, list[float | None]] = {
        "open": [candle.open for candle in candles],
        "high": [candle.high for candle in candles],
        "low": [candle.low for candle in candles],
        "close": [candle.close for candle in candles],
        "volume": [candle.volume for candle in candles],
    }
    if any(candle.oi is not None for candle in candles):
        data["oi"] = [candle.oi for candle in candles]
    frame = pd.DataFrame(data, index=index)
    return frame.sort_index()


def from_frame(frame: pd.DataFrame, *, provider_interval: int = 1) -> tuple[Candle, ...]:
    """Convert an OHLCV frame (any timezone) to normalised candles."""
    if frame is None or frame.empty:
        return ()
    columns = {str(name).lower(): name for name in frame.columns}
    required = ("open", "high", "low", "close")
    if not all(name in columns for name in required):
        raise ValueError("frame is missing OHLC columns")
    volume_name = columns.get("volume")
    oi_name = columns.get("oi") or columns.get("open_interest")
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        index = pd.DatetimeIndex(index)
    if index.tz is None:
        index = index.tz_localize(IST)
    else:
        index = index.tz_convert(IST)
    candles: list[Candle] = []
    for position, stamp in enumerate(index):
        row = frame.iloc[position]
        if any(pd.isna(row[columns[name]]) for name in required):
            continue
        epoch = int(to_ist_ts(stamp).timestamp())
        volume = 0.0
        if volume_name is not None and not pd.isna(row[volume_name]):
            volume = float(row[volume_name])
        oi: float | None = None
        if oi_name is not None and not pd.isna(row[oi_name]):
            oi = float(row[oi_name])
        candles.append(
            Candle(
                time=epoch,
                open=float(row[columns["open"]]),
                high=float(row[columns["high"]]),
                low=float(row[columns["low"]]),
                close=float(row[columns["close"]]),
                volume=volume,
                oi=oi,
            )
        )
    candles.sort(key=lambda candle: candle.time)
    return tuple(candles)


def resample_candles(candles: tuple[Candle, ...], minutes: int) -> tuple[Candle, ...]:
    """Aggregate candles into ``minutes`` buckets, keeping a partial last bucket."""
    if minutes <= 1 or not candles:
        return tuple(candles)
    frame = to_frame(candles)
    aggregation: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    if "oi" in frame.columns:
        aggregation["oi"] = "last"
    result = frame.resample(f"{minutes}min", closed="left", label="left", origin="epoch").agg(
        aggregation
    )
    result = result.dropna(subset=["open", "high", "low", "close"])
    return from_frame(result, provider_interval=minutes)


def heikin_ashi(candles: tuple[Candle, ...] | list[Candle]) -> tuple[dict[str, float], ...]:
    """Heikin-Ashi values, with the conventional first-candle seed."""
    rows: list[dict[str, float]] = []
    previous_open: float | None = None
    previous_close: float | None = None
    for candle in candles:
        ha_close = (candle.open + candle.high + candle.low + candle.close) / 4.0
        if previous_open is None or previous_close is None:
            ha_open = (candle.open + candle.close) / 2.0
        else:
            ha_open = (previous_open + previous_close) / 2.0
        ha_high = max(candle.high, ha_open, ha_close)
        ha_low = min(candle.low, ha_open, ha_close)
        rows.append(
            {
                "time": float(candle.time),
                "open": ha_open,
                "high": ha_high,
                "low": ha_low,
                "close": ha_close,
            }
        )
        previous_open, previous_close = ha_open, ha_close
    return tuple(rows)


def change_percent(candles: tuple[Candle, ...]) -> float | None:
    """Change from the first close of the latest session to the last close."""
    if len(candles) < 2:
        return None
    last = candles[-1]
    latest_day = dt.datetime.fromtimestamp(last.time, tz=IST).date()
    session = [
        candle
        for candle in candles
        if dt.datetime.fromtimestamp(candle.time, tz=IST).date() == latest_day
    ]
    base = session[0].close if session else candles[0].close
    if base == 0:
        return None
    return (last.close - base) / base * 100.0
