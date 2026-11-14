"""Paper-trade demo: replays a synthetic trading day candle-by-candle through the
SAME Engine/rules used live. No Kite account or network needed.
    python demo.py"""
import numpy as np
import pandas as pd
from config import Params
from engine import Engine
from notifier import Notifier


def synthetic_day(seed=7):
    rng = np.random.default_rng(seed)
    prev = pd.date_range("2026-11-13 14:40", periods=50, freq="min")      # warm-up history
    day = pd.date_range("2026-11-16 09:15", periods=151, freq="min")
    idx = prev.append(day)
    # price path: tight opening range -> dip -> rally (breakout) -> plateau -> sell-off
    px_pts = [(0, 100), (50, 100), (65, 100), (95, 99.1), (125, 103), (160, 103), (200, 96)]
    oi_pts = [(0, 1.00e6), (65, 1.00e6), (95, 1.05e6), (125, 1.12e6), (160, 1.12e6), (200, 1.22e6)]
    x = np.arange(len(idx))
    close = np.interp(x, *zip(*px_pts)) + rng.normal(0, 0.06, len(x))
    oi = np.interp(x, *zip(*oi_pts))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + 0.04,
                         "low": np.minimum(open_, close) - 0.04, "close": close,
                         "volume": 1000, "oi": oi}, index=idx)


if __name__ == "__main__":
    p = Params(ema_cross_lookback=5)          # tweak any parameter here
    engine = Engine(p, Notifier(["console"]))
    data = synthetic_day()
    engine.merge("DEMO:FUT", data.iloc[:50])  # seed with previous-day candles
    sigs = []
    for ts, r in data.iloc[50:].iterrows():
        s = engine.on_candle("DEMO:FUT", {"time": ts, **r.to_dict()})
        if s:
            sigs.append(s)
    print(f"\n{len(sigs)} alert(s) fired; sides: {[s.side for s in sigs]}  (no orders placed)")
    assert {"BUY", "SELL"} <= {s.side for s in sigs}, "demo expected at least one BUY and one SELL"
