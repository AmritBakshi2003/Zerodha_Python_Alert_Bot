"""Kite Connect data layer: historical bootstrap, websocket ticks -> 1-min candles,
rate-limit retries and gap back-fill after reconnects."""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd

log = logging.getLogger("feed")
FATAL = ("TokenException", "InputException", "PermissionException")


def with_retry(fn, *args, retries=5, base=1.0, **kw):
    """Retry on network errors / rate limits (HTTP 429) with exponential backoff.
    Auth errors are re-raised immediately (retrying can't fix an expired token)."""
    for i in range(retries):
        try:
            return fn(*args, **kw)
        except Exception as e:
            if type(e).__name__ in FATAL or i == retries - 1:
                raise
            wait = base * 2 ** i
            log.warning("%s failed (%s); retry in %.1fs", getattr(fn, "__name__", fn), e, wait)
            time.sleep(wait)


def fetch_history(kite, token: int, days: int = 3) -> pd.DataFrame:
    now = datetime.now()
    rows = with_retry(kite.historical_data, token, now - timedelta(days=days), now,
                      "minute", oi=True)
    time.sleep(0.4)                     # Kite historical limit is ~3 req/s
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    if "oi" not in df:
        df["oi"] = 0
    df = df.set_index("time")[["open", "high", "low", "close", "volume", "oi"]]
    # drop the still-forming current minute
    return df[df.index < pd.Timestamp(now).floor("min")]


class CandleBuilder:
    """Aggregates ticks into 1-minute candles. A candle is emitted when the first
    tick of the next minute arrives (so thinly traded symbols emit a bit late)."""
    def __init__(self):
        self.cur = {}

    def update(self, key, ts: pd.Timestamp, price: float, oi: float = 0) -> Optional[dict]:
        minute, c, done = ts.floor("min"), self.cur.get(key), None
        if c and minute < c["time"]:
            return None                                  # late/out-of-order tick
        if c is None or minute > c["time"]:
            done = c
            self.cur[key] = {"time": minute, "open": price, "high": price,
                             "low": price, "close": price, "volume": 0, "oi": oi}
        else:
            c["high"], c["low"], c["close"] = max(c["high"], price), min(c["low"], price), price
            if oi:
                c["oi"] = oi
        return done

    def reset(self):
        self.cur.clear()


class KiteFeed:
    def __init__(self, kite, api_key, access_token, token_map: dict, engine):
        from kiteconnect import KiteTicker
        self.kite, self.engine = kite, engine
        self.tok2sym = {t: s for s, t in token_map.items()}
        self.builder, self.first_connect = CandleBuilder(), True
        self.ws = KiteTicker(api_key, access_token, reconnect=True,
                             reconnect_max_tries=50, reconnect_max_delay=60)
        self.ws.on_ticks, self.ws.on_connect = self._on_ticks, self._on_connect
        self.ws.on_close, self.ws.on_error = self._on_close, self._on_error
        self.ws.on_reconnect = lambda ws, n: log.warning("reconnecting (attempt %s)", n)
        self.ws.on_noreconnect = lambda ws: log.error("gave up reconnecting — restart the bot")

    def run(self):
        self.ws.connect(threaded=False)

    def _on_connect(self, ws, _resp):
        toks = list(self.tok2sym)
        ws.subscribe(toks)
        ws.set_mode(ws.MODE_FULL, toks)      # FULL mode carries OI
        log.info("subscribed to %d instruments", len(toks))
        if not self.first_connect:           # fill the hole left by the disconnect
            self.builder.reset()
            for tok, sym in self.tok2sym.items():
                try:
                    self.engine.merge(sym, fetch_history(self.kite, tok, days=1))
                except Exception:
                    log.exception("back-fill failed for %s", sym)
        self.first_connect = False

    def _on_ticks(self, ws, ticks):
        for t in ticks:                      # never let an exception kill the socket thread
            try:
                sym = self.tok2sym.get(t["instrument_token"])
                if not sym or "last_price" not in t:
                    continue
                ts = pd.Timestamp(t.get("exchange_timestamp") or t.get("last_trade_time")
                                  or datetime.now()).tz_localize(None)
                candle = self.builder.update(sym, ts, t["last_price"], t.get("oi", 0))
                if candle:
                    self.engine.on_candle(sym, candle)
            except Exception:
                log.exception("tick handling error")

    def _on_close(self, ws, code, reason):
        log.warning("websocket closed: %s %s", code, reason)

    def _on_error(self, ws, code, reason):
        log.error("websocket error: %s %s", code, reason)
