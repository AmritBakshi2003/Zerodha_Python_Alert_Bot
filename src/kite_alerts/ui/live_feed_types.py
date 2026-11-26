"""Pure value types and helpers for the live feed (F12 / P5).

These have no dependency on the transport or on ``LiveFeedService``; extracting
them keeps ``live_feed.py`` focused on the socket/state machine. Every name is
re-exported from ``live_feed.py``, so no caller needs to change its imports.
"""

from __future__ import annotations

import contextlib
import queue
import random
import threading
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Limits and states (kept here so the value layer has a single source)
# ---------------------------------------------------------------------------
MAX_SUBSCRIPTIONS = 25  # well inside Kite's 3 000 limit
_MAX_SSE_QUEUE = 500  # drop-oldest; prevents a slow browser tab growing memory
_MAX_SSE_CLIENTS = 50  # concurrent chart streams per process
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_LIVE = "live"
STATE_STALE = "stale"
STATE_TOKEN_EXPIRED = "token_expired"
STATE_NO_PLAN = "no_plan"
STATE_MARKET_CLOSED = "market_closed"
STATE_SIMULATED = "simulated"


@dataclass(frozen=True)
class FeedStatus:
    state: str  # one of STATE_* constants
    subscribed: int = 0
    reconnect_count: int = 0
    last_tick_age: float | None = None  # seconds since last tick
    last_tick_time: str = ""  # HH:MM:SS IST
    latency_ms: float | None = None  # exchange timestamp → local receipt (ms)
    reason: str = ""  # human-readable note

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "subscribed": self.subscribed,
            "reconnect_count": self.reconnect_count,
            "last_tick_age": (
                round(self.last_tick_age, 1) if self.last_tick_age is not None else None
            ),
            "last_tick_time": self.last_tick_time,
            "latency_ms": (round(self.latency_ms, 1) if self.latency_ms is not None else None),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FeedAvailability:
    """The single answer to "can this source be used, and how must it be labelled?"."""

    state: str
    available: bool
    enabled: bool
    badge: str
    notice: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "available": self.available,
            "enabled": self.enabled,
            "badge": self.badge,
            "notice": self.notice,
            "reason": self.reason,
        }


#: Plain-language text per state. One table, so no surface can invent its own.
_AVAILABILITY_TEXT: dict[str, str] = {
    STATE_DISCONNECTED: "Live updates start once you connect to Zerodha.",
    STATE_CONNECTING: "Connecting to the Kite live ticker...",
    STATE_LIVE: "Streaming live ticks from Kite.",
    STATE_STALE: "Connected, but no tick has arrived recently.",
    STATE_MARKET_CLOSED: "Market is closed. Live ticks resume at 09:15 IST.",
    STATE_TOKEN_EXPIRED: "Your Kite session expired. Reconnect to use live data.",
    STATE_NO_PLAN: "Your Kite plan has no market data. Delayed NSE is used instead.",
    STATE_SIMULATED: "SIMULATED, not real market data",
}


def _backoff_delay(attempt: int, rng: random.Random) -> float:
    """Exponential backoff with jitter, capped: 1s, 2s, 4s ... 60s (+/- 25%)."""
    base = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** max(0, attempt - 1)))
    return float(base + rng.uniform(0.0, base * 0.25))


#: The badge each state may show. "LIVE" appears here exactly once.
_BADGE_BY_STATE: dict[str, str] = {
    STATE_DISCONNECTED: "OFF",
    STATE_CONNECTING: "CONNECTING",
    STATE_LIVE: "LIVE",
    STATE_STALE: "STALE",
    STATE_MARKET_CLOSED: "CLOSED",
    STATE_TOKEN_EXPIRED: "EXPIRED",
    STATE_NO_PLAN: "NO PLAN",
    STATE_SIMULATED: "SIMULATED",
}

#: Terminal for a retry with the same credentials.
_PROBE_TERMINAL_STATES = (STATE_TOKEN_EXPIRED, STATE_NO_PLAN)


@dataclass(frozen=True)
class LiveTick:
    """A normalised tick delivered to the SSE queue."""

    symbol: str
    token: int
    price: float
    volume_cum: int | None
    oi: float
    exchange_ts: float  # UTC epoch
    received_at: float  # time.time() on arrival
    is_simulated: bool = False

    @property
    def latency_ms(self) -> float | None:
        """Exchange timestamp → local receipt time in ms.  None if negative/huge."""
        diff_ms = (self.received_at - self.exchange_ts) * 1000.0
        if diff_ms < 0 or diff_ms > 300_000:  # negative or > 5 min → clock skew
            return None
        return diff_ms

    @property
    def latency_note(self) -> str:
        lat = self.latency_ms
        if lat is None:
            return "Your PC clock may be out of sync"
        return f"{lat:.0f} ms"


@dataclass(frozen=True)
class SSEEvent:
    """One server-sent event payload."""

    event: str  # "tick" | "candle" | "status" | "heartbeat" | "gap"
    data: dict[str, Any]
    symbol: str = ""


class TokenRegistry:
    """Bidirectional token ↔ symbol lookup, populated at subscribe time."""

    def __init__(self) -> None:
        self._token_to_symbol: dict[int, str] = {}
        self._symbol_to_token: dict[str, int] = {}
        self._lock = threading.Lock()

    def register(self, symbol: str, token: int) -> None:
        with self._lock:
            self._token_to_symbol[token] = symbol
            self._symbol_to_token[symbol.upper()] = token

    def symbol_for(self, token: int) -> str | None:
        with self._lock:
            return self._token_to_symbol.get(token)

    def token_for(self, symbol: str) -> int | None:
        with self._lock:
            return self._symbol_to_token.get(symbol.upper())

    def all_tokens(self) -> list[int]:
        with self._lock:
            return list(self._token_to_symbol.keys())

    def clear(self) -> None:
        with self._lock:
            self._token_to_symbol.clear()
            self._symbol_to_token.clear()


class SSEClient:
    """One browser SSE connection."""

    def __init__(
        self, symbol: str = "", interval: int = 1, max_queue: int = _MAX_SSE_QUEUE
    ) -> None:
        self.symbol = symbol.upper()
        self.interval = interval
        self._queue: queue.Queue[SSEEvent | None] = queue.Queue(maxsize=max_queue)
        self._closed = False

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def push(self, event: SSEEvent) -> None:
        """Non-blocking push; drop oldest if full (slow browser tab)."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()  # drop oldest
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(event)

    def get(self, timeout: float = 15.0) -> SSEEvent | None:
        """Block until an event arrives or timeout (for the heartbeat)."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed = True
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)  # unblock get()


def _candle_dict(candle: Any, *, is_simulated: bool) -> dict[str, Any]:
    """Convert a data.candle_builder.Candle → chart-compatible dict."""
    from .chart.transforms import ist_label

    epoch = int(candle.time.timestamp()) if hasattr(candle.time, "timestamp") else int(candle.time)
    return {
        "time": epoch,
        "ist": ist_label(epoch),
        "open": float(candle.open),
        "high": float(candle.high),
        "low": float(candle.low),
        "close": float(candle.close),
        "volume": int(candle.volume),
        "oi": float(candle.oi) if candle.oi else None,
        "is_simulated": is_simulated,
    }
