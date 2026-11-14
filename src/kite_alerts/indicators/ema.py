"""Exponential moving average.

``seed="first"`` reproduces the prototype exactly (pandas ``ewm(adjust=False)``,
recursion started from the first observation). ``seed="sma"`` reproduces the
TA-Lib / charting-platform convention, which seeds the recursion with the simple
moving average of the first ``period`` values. Defaults are unchanged on purpose:
switching the seed changes alert behaviour and must be a deliberate, opt-in choice.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_VALID_SEEDS = ("first", "sma")


def ema(series: pd.Series, period: int, *, seed: str = "first") -> pd.Series:
    """EMA of ``series`` over ``period`` bars.

    Values before bar ``period - 1`` are ``NaN`` in both modes.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if seed not in _VALID_SEEDS:
        raise ValueError(f"seed must be one of {_VALID_SEEDS}, got {seed!r}")

    if seed == "first":
        return series.ewm(span=period, adjust=False, min_periods=period).mean()
    return _sma_seeded_ema(series, period)


def _sma_seeded_ema(series: pd.Series, period: int) -> pd.Series:
    values = series.to_numpy(dtype=float, copy=False)
    out = np.full(values.shape, np.nan, dtype=float)
    if values.size >= period:
        alpha = 2.0 / (period + 1.0)
        seed_index = period - 1
        out[seed_index] = float(np.mean(values[:period]))
        for index in range(seed_index + 1, values.size):
            out[index] = alpha * values[index] + (1.0 - alpha) * out[index - 1]
    return pd.Series(out, index=series.index, name=series.name)
