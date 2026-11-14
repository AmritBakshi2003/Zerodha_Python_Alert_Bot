"""Holds per-symbol candle history, runs the rules on every closed candle,
applies cooldown, and dispatches alerts. Used by both live feed and demo."""
import logging
import pandas as pd
from config import Params
from signals import evaluate

log = logging.getLogger("engine")


class Engine:
    def __init__(self, params: Params, notifier):
        self.p, self.notifier = params, notifier
        self.frames = {}        # symbol -> DataFrame(open,high,low,close,volume,oi)
        self.last_alert = {}    # symbol -> (side, time)

    def merge(self, symbol: str, df: pd.DataFrame):
        """Seed or back-fill history without triggering alerts."""
        old = self.frames.get(symbol)
        df = df if old is None else pd.concat([old, df])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        self.frames[symbol] = df.tail(self.p.max_bars)

    def on_candle(self, symbol: str, candle: dict):
        """Call with each *closed* 1-minute candle. Returns Signal or None."""
        row = pd.DataFrame([candle]).set_index("time")
        self.merge(symbol, row)
        try:
            sig = evaluate(symbol, self.frames[symbol], self.p)
        except Exception:
            log.exception("rule evaluation failed for %s", symbol)
            return None
        if sig and self._cooldown_ok(sig):
            self.last_alert[symbol] = (sig.side, sig.time)
            self.notifier.send(sig.message())
            return sig
        return None

    def _cooldown_ok(self, sig) -> bool:
        prev = self.last_alert.get(sig.symbol)
        if not prev or prev[0] != sig.side:
            return True
        return (sig.time - prev[1]) >= pd.Timedelta(minutes=self.p.cooldown_minutes)
