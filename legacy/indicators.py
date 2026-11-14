"""Pure-pandas indicator maths (no TA-Lib needed). Each function is stateless."""
from datetime import datetime, time
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    d = close.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + au / ad.replace(0, np.nan))
    out = out.where(ad != 0, 100.0)
    return out.where(au.notna())


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def opening_range(df: pd.DataFrame, minutes: int = 15):
    """(high, low) of today's first `minutes` after 09:15 IST, or None if the
    range isn't complete yet. Index must be tz-naive IST timestamps."""
    day = df.index[-1].date()
    start = pd.Timestamp(datetime.combine(day, time(9, 15)))
    end = start + pd.Timedelta(minutes=minutes)
    if df.index[-1] < end:          # need at least one candle after the range
        return None
    rng = df[(df.index >= start) & (df.index < end)]
    if rng.empty:
        return None
    return float(rng["high"].max()), float(rng["low"].min())
