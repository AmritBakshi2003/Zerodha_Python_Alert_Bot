"""The single owner of all mutable market state.

One thread consumes ticks and the clock:

* the websocket callback only enqueues a ``Tick`` (bounded queue, drops counted),
  so a burst can never block or crash the socket thread;
* this thread owns the candle builder, the engine and the watchdog, which is why
  no scattered locks are needed for correctness (the engine still locks for the
  health snapshot);
* minute boundaries are driven by the clock, independent of ticks;
* history back-fill runs here, never on the websocket thread;
* auth failures stop the run with one alert instead of looping silently.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..config.settings import Settings
from ..core.calendar import TradingCalendar
from ..core.clock import Clock, SystemClock, seconds_until_next_minute, to_ist_ts
from ..core.errors import classify_exception
from ..core.models import Candle, Tick
from ..core.ports import MetricsPort, gauge, metric
from ..data.candle_builder import CandleBuilder
from ..data.history import HistoryClient
from ..data.watchdog import FeedWatchdog
from ..notifiers.hub import NotifierHub
from .engine import Engine

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Command:
    """Control message processed in order with the ticks."""

    kind: str  # "backfill"
    payload: Any = None


class MarketWorker:
    def __init__(
        self,
        *,
        builder: CandleBuilder,
        engine: Engine,
        calendar: TradingCalendar,
        settings: Settings,
        hub: NotifierHub,
        metrics: MetricsPort | None = None,
        clock: Clock | None = None,
        history: HistoryClient | None = None,
        token_map: Mapping[str, int] | None = None,
        futures: frozenset[str] | set[str] = frozenset(),
        warmup_days: int = 4,
        watchdog: FeedWatchdog | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._builder = builder
        self._engine = engine
        self._calendar = calendar
        self._settings = settings
        self._hub = hub
        self._metrics = metrics
        self._clock = clock or SystemClock()
        self._history = history
        self._token_map = dict(token_map or {})
        self._futures = frozenset(futures)
        self._warmup_days = warmup_days
        self._watchdog = watchdog
        self._stop_event = stop_event or threading.Event()
        self._queue: queue.Queue[object] = queue.Queue(maxsize=settings.resilience.tick_queue_size)
        self._feed: Any | None = None
        self._thread: threading.Thread | None = None
        self._last_heartbeat: pd.Timestamp | None = None
        self.auth_failed = False
        self.auth_message = ""
        self.reconnects = 0

    # ------------------------------------------------------------------ wiring
    def attach_feed(self, feed: Any) -> None:
        self._feed = feed

    @property
    def feed(self) -> Any | None:
        return self._feed

    @property
    def token_map(self) -> Mapping[str, int]:
        return dict(self._token_map)

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------ producer
    def enqueue_tick(self, tick: Tick) -> None:
        """Called from the websocket thread: never blocks, never raises."""
        try:
            self._queue.put_nowait(tick)
        except queue.Full:
            metric(self._metrics, "ticks_dropped")
            gauge(self._metrics, "tick_queue_depth", float(self._queue.qsize()))
            log.warning(
                "tick queue full (%d); dropping tick for %s", self._queue.maxsize, tick.symbol
            )

    def request_backfill(self, *, first_connect: bool) -> None:
        self._queue.put(Command("backfill", first_connect))

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name="kite-alerts-worker", daemon=True)
        self._thread.start()

    def run(self) -> None:
        log.info("worker started (queue=%d)", self._queue.maxsize)
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception:  # keep the loop alive; a bug is logged with context
                metric(self._metrics, "worker_errors")
                log.exception("worker step failed")
        log.info("worker stopped")

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # ------------------------------------------------------------------ stepping
    def step(self, timeout: float | None = None) -> bool:
        """Process at most one queue item, then always run the clock actions.

        ``timeout=0`` is used by tests for a deterministic, non-blocking step.
        """
        now = to_ist_ts(self._clock.now())
        if timeout is None:
            timeout = min(seconds_until_next_minute(now), 1.0)

        item: object | None = None
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            item = None

        try:
            if isinstance(item, Tick):
                self._handle_tick(item)
            elif isinstance(item, Command):
                self._handle_command(item)
        finally:
            if item is not None:
                self._queue.task_done()
            gauge(self._metrics, "tick_queue_depth", float(self._queue.qsize()))

        self._tick_clock()
        return True

    def _handle_tick(self, tick: Tick) -> None:
        if self._watchdog is not None:
            self._watchdog.on_tick(tick.symbol, tick.ts)
        if not self._calendar.in_session(tick.ts):
            metric(self._metrics, "ticks_out_of_session", 1, symbol=tick.symbol)
            return
        try:
            candles = self._builder.on_tick(
                tick.symbol,
                tick.ts,
                tick.price,
                volume_cum=tick.volume_cum,
                oi=tick.oi,
            )
        except Exception:
            metric(self._metrics, "candle_errors", 1, symbol=tick.symbol)
            log.exception("candle aggregation failed for %s", tick.symbol)
            return
        for candle in candles:
            self._deliver(candle)

    def _deliver(self, candle: Candle) -> None:
        """Hand one closed candle to the engine; a bad candle never kills the loop.

        ``run_live`` drives this worker through :meth:`step`, so an exception that
        escapes ``on_candle`` would unwind the run loop and stop the bot for the
        whole session. Evaluation bugs stay logged and counted instead.
        """
        try:
            self._engine.on_candle(candle)
        except Exception:
            metric(self._metrics, "engine_errors", 1, symbol=candle.symbol)
            log.exception("engine evaluation failed for %s at %s", candle.symbol, candle.time)

    def _handle_command(self, command: Command) -> None:
        if command.kind == "backfill":
            self._backfill(first_connect=bool(command.payload))
        else:  # pragma: no cover - defensive
            log.warning("unknown worker command %r", command.kind)

    def _tick_clock(self) -> None:
        now = to_ist_ts(self._clock.now())
        for candle in self._builder.close_completed(now):
            self._deliver(candle)

        if self._watchdog is not None:
            actions = self._watchdog.check(now)
            for message in actions.messages:
                self._hub.send_text(message, level="system")
            if actions.reconnect:
                self._reconnect()

        self._maybe_heartbeat(now)

    # ------------------------------------------------------------------ resilience
    def _reconnect(self) -> None:
        feed = self._feed
        if feed is None:
            return
        metric(self._metrics, "feed_reconnects")
        self.reconnects += 1
        log.warning("requesting controlled websocket reconnect (#%d)", self.reconnects)
        try:
            feed.stop()
            feed.start()
            if self._watchdog is not None:
                self._watchdog.note_connected()
            self.request_backfill(first_connect=False)
        except Exception:
            metric(self._metrics, "reconnect_errors")
            log.exception("controlled reconnect failed")

    def _backfill(self, *, first_connect: bool) -> None:
        if self._history is None or not self._token_map:
            return
        now = to_ist_ts(self._clock.now())
        if not self._calendar.in_session(now):
            log.debug("skipping back-fill outside the session")
            return
        days = self._warmup_days if first_connect else 1
        if not first_connect:
            self._builder.reset()  # in-progress candles are unreliable across a gap
        for symbol, token in self._token_map.items():
            if self._stop_event.is_set():
                return
            try:
                frame = self._history.fetch(token, days=days, continuous=symbol in self._futures)
            except Exception as exc:
                kind = classify_exception(exc)
                if kind == "auth":
                    self._fail_auth(
                        "Kite access token expired or was revoked during history back-fill. "
                        "Run `kite-alerts login` and restart."
                    )
                    return
                log.warning("back-fill failed for %s (%s): %s", symbol, kind, exc)
                metric(self._metrics, "backfill_failures", 1, symbol=symbol)
                continue
            if frame.empty:
                log.warning("back-fill for %s returned no candles", symbol)
                metric(self._metrics, "backfill_empty", 1, symbol=symbol)
                continue
            bars = self._engine.seed(symbol, frame)
            metric(self._metrics, "backfill_bars", float(len(frame)), symbol=symbol)
            log.info("back-filled %s with %d bar(s) (buffer=%d)", symbol, len(frame), bars)

    def _fail_auth(self, message: str) -> None:
        if self.auth_failed:
            return
        self.auth_failed = True
        self.auth_message = message
        log.error("fatal auth error: %s", message)
        metric(self._metrics, "auth_errors")
        self._hub.send_text(f"🔐 {message}", level="error", dedup_key="auth-failure")
        self._stop_event.set()

    # ------------------------------------------------------------------ heartbeat
    def _maybe_heartbeat(self, now: pd.Timestamp) -> None:
        minutes = self._settings.observability.heartbeat_minutes
        if minutes <= 0 or not self._calendar.in_session(now):
            return
        if self._last_heartbeat is None:
            self._last_heartbeat = now
            return
        if (now - self._last_heartbeat).total_seconds() < minutes * 60:
            return
        self._last_heartbeat = now
        age = self._watchdog.last_tick_age_seconds if self._watchdog is not None else None
        text = (
            f"💓 kite-alerts alive at {now:%H:%M} IST — "
            f"candles {self._counter('candles_processed')}, "
            f"alerts {self._counter('alerts_fired')}, "
            f"reconnects {self.reconnects}, "
            f"last tick {f'{age:.0f}s ago' if age is not None else 'n/a'}"
        )
        self._hub.send_text(text, level="info", dedup_key=f"heartbeat-{now:%Y%m%d%H%M}")

    def _counter(self, name: str) -> float:
        getter = getattr(self._metrics, "get", None)
        if callable(getter):
            try:
                return float(getter(name))
            except Exception:
                return 0.0
        return 0.0

    # ------------------------------------------------------------------ health
    def health(self) -> dict[str, Any]:
        now = to_ist_ts(self._clock.now())
        age = self._watchdog.last_tick_age_seconds if self._watchdog is not None else None
        return {
            "session": self._calendar.describe(now),
            "in_session": self._calendar.in_session(now),
            "last_tick_age_seconds": None if age is None else round(age, 1),
            "queue_depth": self.queue_depth,
            "reconnects": self.reconnects,
            "auth_failed": self.auth_failed,
            "engine": dict(self._engine.stats()),
            "in_progress_candles": {
                symbol: ts.isoformat() for symbol, ts in self._builder.in_progress().items()
            },
        }
