"""Per-symbol candle history, evaluation, cooldown and dispatch.

Changed versus the prototype, all of it observable in the audit log:

* alert state is read from (and written to) the durable store, so a restart does
  not re-send yesterday's alert;
* a candle timestamp is processed at most once (reconnect back-fill overlaps the
  live stream);
* evaluation and dispatch happen under one lock, and a notification is enqueued,
  never sent inline;
* out-of-session candles are ignored rather than alerting after the close.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd

from ..config.settings import StrategyParams
from ..core.calendar import TradingCalendar
from ..core.models import Alert, Candle
from ..core.ports import AlertSinkPort, AlertStorePort, MetricsPort, gauge, metric
from ..data.history import merge_frames
from ..strategy.base import RuleRegistry
from ..strategy.evaluator import evaluate

log = logging.getLogger(__name__)


class Engine:
    """Turn closed candles into (at most one) alert per candle."""

    def __init__(
        self,
        *,
        params_for: Callable[[str], StrategyParams],
        store: AlertStorePort,
        sink: AlertSinkPort,
        calendar: TradingCalendar,
        metrics: MetricsPort | None = None,
        registry: RuleRegistry | None = None,
    ) -> None:
        self._params_for = params_for
        self._store = store
        self._sink = sink
        self._calendar = calendar
        self._metrics = metrics
        self._registry = registry
        self._frames: dict[str, pd.DataFrame] = {}
        self._last_processed: dict[str, pd.Timestamp] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ history
    def seed(self, symbol: str, frame: pd.DataFrame) -> int:
        """Merge history without evaluating it. Returns the resulting bar count."""
        params = self._params_for(symbol)
        with self._lock:
            merged = merge_frames(self._frames.get(symbol), frame, params.max_bars)
            if merged.empty:
                return 0
            self._frames[symbol] = merged
            stored = self._store.last_candle(symbol)
            newest = merged.index[-1]
            last = newest if stored is None or newest > stored else stored
            self._last_processed[symbol] = last
            if stored is None or last > stored:
                self._store.set_last_candle(symbol, last)
            gauge(self._metrics, "bars_buffered", float(len(merged)), symbol=symbol)
            return len(merged)

    def frame(self, symbol: str) -> pd.DataFrame | None:
        with self._lock:
            frame = self._frames.get(symbol)
            return None if frame is None else frame.copy()

    def last_processed(self, symbol: str) -> pd.Timestamp | None:
        with self._lock:
            return self._last_processed.get(symbol)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                symbol: {
                    "bars": len(frame),
                    "last_candle": (frame.index[-1].isoformat() if len(frame) else None),
                    "last_close": float(frame["close"].iloc[-1]) if len(frame) else None,
                }
                for symbol, frame in self._frames.items()
            }

    # ------------------------------------------------------------------ evaluation
    def on_candle(self, candle: Candle) -> Alert | None:
        """Evaluate one *closed* candle. Returns the alert if one fired."""
        if not self._calendar.in_session(candle.time):
            metric(self._metrics, "candles_out_of_session")
            log.debug("%s: candle %s outside session; ignored", candle.symbol, candle.time)
            return None

        params = self._params_for(candle.symbol)
        symbol = candle.symbol

        with self._lock:
            last = self._last_processed.get(symbol)
            if last is not None and candle.time <= last:
                metric(self._metrics, "candles_duplicate")
                return None

            merged = merge_frames(self._frames.get(symbol), candle.to_row(), params.max_bars)
            self._frames[symbol] = merged
            self._last_processed[symbol] = candle.time
            self._store.set_last_candle(symbol, candle.time)
            metric(self._metrics, "candles_processed", 1, symbol=symbol)

            try:
                alert = evaluate(
                    symbol,
                    merged,
                    params,
                    session=self._calendar.window,
                    registry=self._registry,
                )
            except Exception:
                metric(self._metrics, "evaluation_errors", 1, symbol=symbol)
                log.exception("rule evaluation failed for %s", symbol)
                return None

            if alert is None:
                return None

            if not self._cooldown_ok(alert, params):
                metric(self._metrics, "alerts_suppressed_cooldown", 1, symbol=symbol)
                log.info(
                    "%s %s suppressed by cooldown (%.0f min)",
                    symbol,
                    alert.side.value,
                    params.cooldown_minutes,
                )
                return None

            if not self._store.record_alert(alert):
                metric(self._metrics, "alerts_duplicate", 1, symbol=symbol)
                log.warning("%s: duplicate alert for %s suppressed", symbol, alert.ts)
                return None

            metric(self._metrics, "alerts_fired", 1, symbol=symbol)
            log.info(
                "ALERT %s %s @ %.2f | %s",
                alert.side.value,
                symbol,
                alert.price,
                "; ".join(alert.reasons),
            )

        # Enqueue outside the lock; the hub never blocks the caller.
        try:
            self._sink.submit(alert)
        except Exception:  # pragma: no cover - sink is the hub, which never raises
            log.exception("failed to dispatch alert for %s", symbol)
        return alert

    def _cooldown_ok(self, alert: Alert, params: StrategyParams) -> bool:
        """Prototype semantics: cooldown per side; a side flip resets it.

        ``min_side_flip_minutes`` (default 0) optionally damps flip-flopping.
        """
        previous_side, previous_ts = self._store.last_alert(alert.symbol)
        if previous_side is None or previous_ts is None:
            return True
        gap_minutes = float((alert.ts - previous_ts).total_seconds()) / 60.0
        if previous_side != alert.side.value:
            return gap_minutes >= params.min_side_flip_minutes
        return gap_minutes >= params.cooldown_minutes

    # ------------------------------------------------------------------ diagnostics
    def stats(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "symbols": len(self._frames),
                "bars": sum(len(frame) for frame in self._frames.values()),
            }
