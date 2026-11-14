"""Tick -> one-minute candle aggregation.

Two things the prototype got wrong, both fixed here:

* **volume** - Kite's websocket sends ``volume_traded`` as a *cumulative* counter
  for the day, so a per-candle volume has to be derived from the delta between the
  first and last tick of the minute. It is never copied (which reported a whole
  day's volume on every candle) and never allowed to go negative (a feed or
  session reset makes the counter jump backwards).
* **closing** - the prototype only emitted a candle when the *next* minute's first
  tick arrived, so the last candle of an illiquid symbol never closed at all. A
  candle now also closes on the clock via :meth:`CandleBuilder.close_completed`,
  which ``MarketWorker._tick_clock`` calls on every loop iteration.

Ordering rules, all enforced under one lock because ticks arrive on Kite's
websocket thread while the clock runs on the worker thread:

* a tick older than the candle in progress is *late* and dropped;
* a tick identical to the previous one (same timestamp **and** price) is a
  *duplicate* and dropped;
* a minute that was already emitted is never emitted again, even after
  :meth:`reset` - that is what stops a reconnect back-fill from producing a
  second alert for a candle that was already alerted.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

import pandas as pd

from ..core.clock import floor_minute, to_ist_ts
from ..core.models import Candle
from ..core.ports import MetricsPort, metric

#: How many emitted minutes to remember per symbol (a full session plus slack).
_EMITTED_MEMORY = 600


@dataclass(slots=True)
class _Open:
    """The candle currently being built for one symbol."""

    time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    first_cum: int | None
    last_cum: int | None
    oi: float

    def as_candle(self, symbol: str) -> Candle:
        volume = 0
        if self.first_cum is not None and self.last_cum is not None:
            volume = max(0, int(self.last_cum) - int(self.first_cum))
        return Candle(
            symbol=symbol,
            time=self.time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=volume,
            oi=self.oi,
        )


class CandleBuilder:
    """Aggregate ticks into closed one-minute :class:`Candle` objects."""

    def __init__(self, *, metrics: MetricsPort | None = None) -> None:
        self._metrics = metrics
        self._lock = threading.RLock()
        self._open: dict[str, _Open] = {}
        self._last_seen: dict[str, tuple[pd.Timestamp, float]] = {}
        self._emitted: dict[str, set[pd.Timestamp]] = {}
        self._emitted_order: dict[str, deque[pd.Timestamp]] = {}

    # ------------------------------------------------------------------ producer
    def on_tick(
        self,
        symbol: str,
        ts: pd.Timestamp | object,
        price: float,
        *,
        volume_cum: int | None = None,
        oi: float = 0.0,
    ) -> list[Candle]:
        """Apply one tick; return any candle this tick closed.

        Returns at most one candle (the previous minute) - a single tick can only
        ever close the minute before it.
        """
        moment = to_ist_ts(ts)
        minute = floor_minute(moment)
        value = float(price)
        cumulative = None if volume_cum is None else int(volume_cum)

        with self._lock:
            current = self._open.get(symbol)
            if current is not None and minute < current.time:
                metric(self._metrics, "late_ticks", 1, symbol=symbol)
                return []
            if current is not None and minute > current.time:
                finished = self._finish(symbol, current)
                current = None
            else:
                finished = None

            if current is None:
                if self._already_emitted(symbol, minute):
                    metric(self._metrics, "replayed_ticks", 1, symbol=symbol)
                    return [finished] if finished is not None else []
                current = _Open(
                    time=minute,
                    open=value,
                    high=value,
                    low=value,
                    close=value,
                    first_cum=cumulative,
                    last_cum=cumulative,
                    oi=float(oi or 0.0),
                )
                self._open[symbol] = current
            else:
                seen = self._last_seen.get(symbol)
                if seen is not None and seen[0] == moment and seen[1] == value:
                    metric(self._metrics, "duplicate_ticks", 1, symbol=symbol)
                    return [finished] if finished is not None else []
                current.high = max(current.high, value)
                current.low = min(current.low, value)
                current.close = value
                if cumulative is not None:
                    if current.first_cum is None:
                        current.first_cum = cumulative
                    current.last_cum = cumulative
                if oi:
                    current.oi = float(oi)

            self._last_seen[symbol] = (moment, value)
            return [finished] if finished is not None else []

    # ------------------------------------------------------------------ clock
    def close_completed(self, now: pd.Timestamp | object) -> list[Candle]:
        """Close every candle whose minute ended before ``now`` (time order)."""
        boundary = floor_minute(to_ist_ts(now))
        with self._lock:
            due = sorted(
                (state.time, symbol)
                for symbol, state in self._open.items()
                if state.time < boundary
            )
            return [self._finish(symbol, self._open[symbol]) for _time, symbol in due]

    def pending(self, now: pd.Timestamp | object) -> tuple[Candle, ...]:
        """Completed-but-not-yet-emitted candles, without consuming them."""
        boundary = floor_minute(to_ist_ts(now))
        with self._lock:
            due = sorted(
                (state.time, symbol)
                for symbol, state in self._open.items()
                if state.time < boundary
            )
            return tuple(self._open[symbol].as_candle(symbol) for _time, symbol in due)

    # ------------------------------------------------------------------ state
    def in_progress(self) -> dict[str, pd.Timestamp]:
        """``symbol -> minute`` of every candle currently being built."""
        with self._lock:
            return {symbol: state.time for symbol, state in self._open.items()}

    def reset(self, symbol: str | None = None) -> None:
        """Drop in-progress candles because their state is unreliable across a gap.

        Already-emitted minutes are deliberately remembered so a back-fill cannot
        re-emit a candle that already produced an alert.
        """
        with self._lock:
            if symbol is None:
                self._open.clear()
                self._last_seen.clear()
            else:
                self._open.pop(symbol, None)
                self._last_seen.pop(symbol, None)

    # ------------------------------------------------------------------ internals
    def _finish(self, symbol: str, state: _Open) -> Candle:
        candle = state.as_candle(symbol)
        self._open.pop(symbol, None)
        self._remember(symbol, candle.time)
        metric(self._metrics, "candles_closed", 1, symbol=symbol)
        return candle

    def _already_emitted(self, symbol: str, minute: pd.Timestamp) -> bool:
        return minute in self._emitted.get(symbol, frozenset())

    def _remember(self, symbol: str, minute: pd.Timestamp) -> None:
        seen = self._emitted.setdefault(symbol, set())
        order = self._emitted_order.setdefault(symbol, deque())
        if minute in seen:
            return
        seen.add(minute)
        order.append(minute)
        while len(order) > _EMITTED_MEMORY:
            seen.discard(order.popleft())
