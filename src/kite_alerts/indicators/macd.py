"""MACD line, signal line and histogram (prototype-identical by default)."""

from __future__ import annotations

import pandas as pd

from .ema import ema


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    *,
    seed: str = "first",
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return ``(macd_line, signal_line, histogram)``."""
    if fast >= slow:
        raise ValueError(f"fast ({fast}) must be < slow ({slow})")
    line = ema(close, fast, seed=seed) - ema(close, slow, seed=seed)
    signal_line = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, signal_line, line - signal_line
