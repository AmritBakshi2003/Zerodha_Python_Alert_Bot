"""Stale-feed watchdog.

The prototype had no way to notice a feed that had silently stopped: the socket
stayed "connected", ticks stopped arriving, and the bot sat idle until somebody
looked at it. This watchdog turns that into an explicit, bounded reaction:

1. one notice when the feed has produced nothing for ``FEED_STALE_SECONDS``;
2. one *controlled* reconnect once the silence has persisted for a second window;
3. at most one reconnect per three windows - a reconnect loop against Kite gets the
   API key throttled, which is worse than a late recovery;
4. per-symbol notices for instruments that go quiet while others keep ticking;
5. complete silence outside the session, when no data is expected at all.

Staleness is measured on **event time** against the latest of: the session open,
the last (re)connect and the newest tick. Measuring from the session open is what
makes "the socket connected at 09:00 but never delivered anything" visible at
09:16 instead of looking like 16 minutes of silence the moment it connects.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import pandas as pd

from ..core.calendar import TradingCalendar
from ..core.clock import Clock, SystemClock, to_ist_ts
from ..core.ports import MetricsPort, metric

log = logging.getLogger(__name__)

#: Staleness must persist this many ``feed_stale_seconds`` windows before reconnecting.
STALE_WINDOWS_BEFORE_RECONNECT = 2

#: A reconnect is only allowed once this many windows have passed since the last one.
RECONNECT_COOLDOWN_WINDOWS = 3


@dataclass(frozen=True, slots=True)
class WatchdogActions:
    """What the worker should do after a watchdog check."""

    reconnect: bool = False
    messages: tuple[str, ...] = ()


class FeedWatchdog:
    """Detect a silent feed and ask for a controlled reconnect."""

    def __init__(
        self,
        *,
        calendar: TradingCalendar,
        symbols: tuple[str, ...] | list[str] = (),
        feed_stale_seconds: float = 120.0,
        symbol_stale_seconds: float = 600.0,
        clock: Clock | None = None,
        metrics: MetricsPort | None = None,
    ) -> None:
        self._calendar = calendar
        self._symbols = tuple(symbols)
        self._feed_stale_seconds = float(feed_stale_seconds)
        self._symbol_stale_seconds = float(symbol_stale_seconds)
        self._clock: Clock = clock or SystemClock()
        self._metrics = metrics
        self._lock = threading.RLock()
        self._last_tick: dict[str, pd.Timestamp] = {}
        self._connected_at: pd.Timestamp | None = None
        self._stale_since: pd.Timestamp | None = None
        self._last_reconnect: pd.Timestamp | None = None
        self._notified_symbols: set[str] = set()

    # ------------------------------------------------------------------ inputs
    def on_tick(self, symbol: str, ts: pd.Timestamp | object) -> None:
        """Record that ``symbol`` produced a tick - the feed is alive again."""
        with self._lock:
            self._last_tick[symbol] = to_ist_ts(ts)
            self._stale_since = None
            self._notified_symbols.discard(symbol)

    def note_connected(self) -> None:
        """Start a fresh staleness episode after a (re)connect."""
        with self._lock:
            self._connected_at = to_ist_ts(self._clock.now())
            self._stale_since = None

    # ------------------------------------------------------------------ outputs
    @property
    def last_tick_age_seconds(self) -> float | None:
        """Age of the newest tick across the watchlist, or ``None`` if none yet."""
        with self._lock:
            if not self._last_tick:
                return None
            newest = max(self._last_tick.values())
        return max(0.0, float((to_ist_ts(self._clock.now()) - newest).total_seconds()))

    def check(self, now: pd.Timestamp | object) -> WatchdogActions:
        """Evaluate staleness at ``now`` and decide whether to reconnect."""
        moment = to_ist_ts(now)
        if not self._calendar.in_session(moment):
            with self._lock:
                self._stale_since = None
            return WatchdogActions()

        with self._lock:
            age = self._effective_age(moment)
            messages: list[str] = []
            reconnect = False

            if age < self._feed_stale_seconds:
                self._stale_since = None
            elif self._stale_since is None:
                self._stale_since = moment
                messages.append(self._first_notice(moment, age))
                metric(self._metrics, "feed_stale_notices")
            elif self._window_elapsed(moment) and self._cooldown_elapsed(moment):
                messages.append(
                    f"Feed stale: no tick for {age:.0f}s "
                    f"(limit {self._feed_stale_seconds:.0f}s) across "
                    f"{len(self._symbols)} symbol(s); reconnecting the websocket"
                )
                reconnect = True
                self._last_reconnect = moment
                self._stale_since = moment
                metric(self._metrics, "feed_reconnect_requests")

            messages.extend(self._symbol_notices(moment))
            return WatchdogActions(reconnect=reconnect, messages=tuple(messages))

    # ------------------------------------------------------------------ internals
    def _effective_age(self, moment: pd.Timestamp) -> float:
        session_start, _ = self._calendar.session_bounds(moment)
        references = [session_start]
        if self._connected_at is not None:
            references.append(self._connected_at)
        if self._last_tick:
            references.append(max(self._last_tick.values()))
        return max(0.0, float((moment - max(references)).total_seconds()))

    def _first_notice(self, moment: pd.Timestamp, age: float) -> str:
        if not self._last_tick:
            session_start, _ = self._calendar.session_bounds(moment)
            return (
                f"No market data received since the {session_start:%H:%M} open "
                f"({age:.0f}s); check the websocket and the access token"
            )
        quiet = [symbol for symbol in self._symbols if symbol in self._last_tick]
        return (
            f"No market data for {age:.0f}s on "
            f"{', '.join(quiet) or 'the watchlist'} "
            f"(limit {self._feed_stale_seconds:.0f}s)"
        )

    def _window_elapsed(self, moment: pd.Timestamp) -> bool:
        if self._stale_since is None:
            return False
        elapsed = float((moment - self._stale_since).total_seconds())
        return elapsed >= STALE_WINDOWS_BEFORE_RECONNECT * self._feed_stale_seconds

    def _cooldown_elapsed(self, moment: pd.Timestamp) -> bool:
        if self._last_reconnect is None:
            return True
        elapsed = float((moment - self._last_reconnect).total_seconds())
        return elapsed >= RECONNECT_COOLDOWN_WINDOWS * self._feed_stale_seconds

    def _symbol_notices(self, moment: pd.Timestamp) -> list[str]:
        if self._symbol_stale_seconds <= 0:
            return []
        notices: list[str] = []
        for symbol in self._symbols:
            last = self._last_tick.get(symbol)
            if last is None or symbol in self._notified_symbols:
                continue
            age = max(0.0, float((moment - last).total_seconds()))
            if age < self._symbol_stale_seconds:
                continue
            self._notified_symbols.add(symbol)
            log.warning("%s has been quiet for %.0fs", symbol, age)
            metric(self._metrics, "symbol_stale_notices", 1, symbol=symbol)
            notices.append(
                f"No {symbol} tick for {age / 60.0:.0f} min "
                f"(limit {self._symbol_stale_seconds / 60.0:.0f} min); the instrument may be "
                "illiquid or the contract may have rolled"
            )
        return notices
