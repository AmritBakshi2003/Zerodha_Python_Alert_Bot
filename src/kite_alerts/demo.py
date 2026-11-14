"""Offline paper demo: replays a synthetic trading day through the SAME engine.

No Kite account, no network, no credentials::

    python -m kite_alerts.demo

The output format is unchanged from the prototype (BUY and SELL alerts with
reasons), because that is what the client already recognises.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from .config.settings import StrategyParams
from .core.calendar import TradingCalendar
from .core.clock import to_ist_ts
from .core.models import Alert, Candle
from .engine.engine import Engine
from .notifiers.console import ensure_utf8_stream
from .persistence.store import InMemoryAlertStore

SYMBOL = "DEMO:FUT"


def synthetic_day(seed: int = 7) -> pd.DataFrame:
    """Warm-up history plus one session: tight open, dip, breakout, sell-off."""
    rng = np.random.default_rng(seed)
    previous = pd.date_range("2026-11-13 14:40", periods=50, freq="min")  # warm-up history
    session = pd.date_range("2026-11-16 09:15", periods=151, freq="min")
    index = previous.append(session)

    price_points = [(0, 100), (50, 100), (65, 100), (95, 99.1), (125, 103), (160, 103), (200, 96)]
    oi_points = [
        (0, 1.00e6),
        (65, 1.00e6),
        (95, 1.05e6),
        (125, 1.12e6),
        (160, 1.12e6),
        (200, 1.22e6),
    ]
    x = np.arange(len(index))
    price_x, price_y = zip(*price_points, strict=False)
    oi_x, oi_y = zip(*oi_points, strict=False)
    close = np.interp(x, price_x, price_y) + rng.normal(0, 0.06, len(x))
    oi = np.interp(x, oi_x, oi_y)
    open_ = np.r_[close[0], close[:-1]]

    ist = pd.DatetimeIndex([to_ist_ts(value) for value in index])
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 0.04,
            "low": np.minimum(open_, close) - 0.04,
            "close": close,
            "volume": 1000,
            "oi": oi,
        },
        index=ist,
    )


class _PrintingSink:
    """Prints each alert exactly as the prototype did, and records it."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def submit(self, alert: Alert) -> None:
        self.alerts.append(alert)
        print(alert.message(), flush=True)


def main() -> int:
    ensure_utf8_stream(newline="\n")  # emoji-safe on Windows and byte-stable output
    params = StrategyParams(ema_cross_lookback=5)
    calendar = TradingCalendar()  # 2026-11-16 is a Monday, inside the session
    sink = _PrintingSink()
    engine = Engine(
        params_for=lambda _symbol: params,
        store=InMemoryAlertStore(),
        sink=sink,
        calendar=calendar,
    )

    data = synthetic_day()
    engine.seed(SYMBOL, data.iloc[:50])  # previous-session candles, no alerts

    for ts, row in data.iloc[50:].iterrows():
        engine.on_candle(
            Candle(
                symbol=SYMBOL,
                time=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
                oi=float(row["oi"]),
            )
        )

    sides = [alert.side.value for alert in sink.alerts]
    print(f"\n{len(sink.alerts)} alert(s) fired; sides: {sides}  (no orders placed)")
    if not {"BUY", "SELL"} <= set(sides):
        print(
            "demo self-check failed: expected at least one BUY and one SELL alert",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
