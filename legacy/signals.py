"""Rule engine. Each rule returns:
     (+1, text)  bullish   |  (-1, text) bearish  |  (0, "") neutral
     None                  rule can't be evaluated (e.g. no OI for equities)
A BUY/SELL fires when >= MIN_AGREE available rules agree and none disagree."""
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

from config import Params
from indicators import ema, rsi, macd, opening_range


@dataclass
class Signal:
    symbol: str
    side: str            # "BUY" | "SELL"
    time: pd.Timestamp
    price: float
    reasons: List[str]

    def message(self) -> str:
        icon = "🟢" if self.side == "BUY" else "🔴"
        return (f"{icon} {self.side} {self.symbol} @ {self.price:.2f} "
                f"[{self.time:%H:%M}] — " + "; ".join(self.reasons))


# ----------------------------- individual rules -----------------------------
def rule_ema(df, p: Params):
    fast, slow = ema(df["close"], p.ema_fast), ema(df["close"], p.ema_slow)
    diff = fast - slow
    if pd.isna(diff.iloc[-1]):
        return None
    recent = diff.iloc[-(p.ema_cross_lookback + 1):-1]
    if diff.iloc[-1] > 0 and (recent <= 0).any():
        return 1, f"EMA{p.ema_fast} crossed above EMA{p.ema_slow}"
    if diff.iloc[-1] < 0 and (recent >= 0).any():
        return -1, f"EMA{p.ema_fast} crossed below EMA{p.ema_slow}"
    return 0, ""


def rule_rsi(df, p: Params):
    r = rsi(df["close"], p.rsi_period).iloc[-1]
    if pd.isna(r):
        return None
    if p.rsi_buy_min <= r <= p.rsi_buy_max:
        return 1, f"RSI {r:.1f} in bullish zone"
    if p.rsi_sell_min <= r <= p.rsi_sell_max:
        return -1, f"RSI {r:.1f} in bearish zone"
    return 0, ""


def rule_macd(df, p: Params):
    line, sig, hist = macd(df["close"], p.macd_fast, p.macd_slow, p.macd_signal)
    if pd.isna(sig.iloc[-1]) or len(hist) < 2:
        return None
    if line.iloc[-1] > sig.iloc[-1] and hist.iloc[-1] > hist.iloc[-2]:
        return 1, "MACD above signal, histogram rising"
    if line.iloc[-1] < sig.iloc[-1] and hist.iloc[-1] < hist.iloc[-2]:
        return -1, "MACD below signal, histogram falling"
    return 0, ""


def rule_orb(df, p: Params):
    rng = opening_range(df, p.orb_minutes)
    if rng is None:
        return 0, ""                      # range not formed yet -> neutral
    hi, lo = rng
    c, buf = df["close"].iloc[-1], p.orb_buffer_pct / 100
    if c > hi * (1 + buf):
        return 1, f"ORB breakout above {hi:.2f}"
    if c < lo * (1 - buf):
        return -1, f"ORB breakdown below {lo:.2f}"
    return 0, ""


def rule_oi(df, p: Params):
    L = p.oi_lookback
    if (not p.oi_enabled or "oi" not in df or len(df) <= L
            or df["oi"].iloc[-1] <= 0 or df["oi"].iloc[-1 - L] <= 0):
        return None
    chg = (df["oi"].iloc[-1] / df["oi"].iloc[-1 - L] - 1) * 100
    px = df["close"].iloc[-1] - df["close"].iloc[-1 - L]
    if chg >= p.oi_min_change_pct:
        if px > 0:
            return 1, f"OI {chg:+.2f}% with price up (long buildup)"
        if px < 0:
            return -1, f"OI {chg:+.2f}% with price down (short buildup)"
    elif chg <= -p.oi_min_change_pct:
        if px > 0:
            return 1, f"OI {chg:+.2f}% with price up (short covering)"
        if px < 0:
            return -1, f"OI {chg:+.2f}% with price down (long unwinding)"
    return 0, ""


RULES = [rule_ema, rule_rsi, rule_macd, rule_orb, rule_oi]


# --------------------------------- evaluator --------------------------------
def evaluate(symbol: str, df: pd.DataFrame, p: Params) -> Optional[Signal]:
    if len(df) < p.warmup_bars:
        return None
    avail = [r for r in (rule(df, p) for rule in RULES) if r is not None]
    bull = [t for v, t in avail if v > 0]
    bear = [t for v, t in avail if v < 0]
    need = min(max(p.min_agree, 1), len(avail))
    if not avail:
        return None
    ts, px = df.index[-1], float(df["close"].iloc[-1])
    if len(bull) >= need and not bear:
        return Signal(symbol, "BUY", ts, px, bull)
    if len(bear) >= need and not bull:
        return Signal(symbol, "SELL", ts, px, bear)
    return None
