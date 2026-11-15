"""Live Kite ticker feed for the chart workstation (C4).

Design decisions
----------------
KiteTicker wraps a Twisted reactor that *cannot be restarted* inside the same
process once it has been stopped.  Running it in a background thread keeps the
reactor alive for the lifetime of the dashboard process, while the feed can be
paused and resumed by subscribing/unsubscribing without ever closing the
websocket until the whole dashboard shuts down.

The feed is intentionally **completely isolated from the alert path**:

* It never imports or calls the notifier, the alert store, the engine or the
  evaluator.
* It owns a *separate* CandleBuilder whose emitted candles go only to the SSE
  queue.  The bot's CandleBuilder (in MarketWorker) is untouched.
* Ticks are sanitised (token → symbol lookup) and pushed into a bounded queue;
  the SSE layer reads the queue.

Connection and subscription limits (documented by Kite):
* Maximum simultaneous websocket connections: 3 per user.  The dashboard opens
  exactly 1.
* Maximum instrument tokens per connection: 3 000.  We cap subscriptions at 25
  by default (chart symbol + watchlist).
* Reconnect attempts: We use our own exponential backoff; KiteTicker's own
  reconnect is used as a signal only.

What is UNVERIFIED
------------------
Everything here that requires a real paid Kite plan has never been tested with
real data.  See docs/live-feed.md for the full list.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..core.calendar import TradingCalendar
from ..core.clock import IST, SystemClock, to_ist_ts
from ..data.candle_builder import CandleBuilder

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits (see module docstring for the documented source)
# ---------------------------------------------------------------------------
MAX_SUBSCRIPTIONS = 25  # well inside Kite's 3 000 limit
_TICK_THROTTLE_PER_SYMBOL = 0.25  # seconds between SSE pushes (max 4/s)
_HEARTBEAT_INTERVAL = 15.0  # seconds
_MAX_SSE_QUEUE = 500  # drop-oldest; prevents a slow browser tab growing memory
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0
_STALE_THRESHOLD = 30.0  # seconds since last tick before badge → stale
_CLOCK_SKEW_WARN = 30.0  # seconds of apparent negative or huge latency

# Feed states exposed through /api/data-sources and the status area
STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_LIVE = "live"
STATE_STALE = "stale"
STATE_TOKEN_EXPIRED = "token_expired"
STATE_NO_PLAN = "no_plan"
STATE_MARKET_CLOSED = "market_closed"
STATE_SIMULATED = "simulated"


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Token-to-symbol registry (populated from the instrument resolver at startup)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# History bootstrap helpers
# ---------------------------------------------------------------------------


def _bootstrap_candles(
    kite: Any,
    token: int,
    symbol: str,
    *,
    days: int = 1,
) -> list[dict[str, Any]]:
    """Fetch recent 1-minute history via kite.historical_data.

    Returns a list of normalised dicts compatible with data.history.normalise_history.
    Returns [] on failure rather than crashing.
    """
    from ..data.history import normalise_history

    try:
        now_ist = dt.datetime.now(tz=IST)
        from_dt = (now_ist - dt.timedelta(days=days)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        to_dt = now_ist
        rows = kite.historical_data(
            token,
            from_dt.replace(tzinfo=None),  # Kite expects naive IST
            to_dt.replace(tzinfo=None),
            "minute",
            False,
            True,  # request OI; will be retried without if unsupported
        )
        frame = normalise_history(rows)
        if frame.empty:
            return []
        result: list[dict[str, Any]] = []
        for ts, row in frame.iterrows():
            epoch = int(to_ist_ts(ts).timestamp())
            result.append(
                {
                    "time": epoch,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row.get("volume", 0)),
                    "oi": float(row.get("oi", 0.0)),
                }
            )
        return result
    except Exception as exc:
        log.info("bootstrap failed for %s (%s)", symbol, type(exc).__name__)
        return []


def _bootstrap_without_oi(kite: Any, token: int, symbol: str) -> list[dict[str, Any]]:
    """Fallback bootstrap without OI for instruments that reject oi=True."""
    from ..data.history import normalise_history

    try:
        now_ist = dt.datetime.now(tz=IST)
        from_dt = (now_ist - dt.timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        to_dt = now_ist
        rows = kite.historical_data(
            token,
            from_dt.replace(tzinfo=None),
            to_dt.replace(tzinfo=None),
            "minute",
            False,
            False,
        )
        frame = normalise_history(rows)
        if frame.empty:
            return []
        result: list[dict[str, Any]] = []
        for ts, row in frame.iterrows():
            epoch = int(to_ist_ts(ts).timestamp())
            result.append(
                {
                    "time": epoch,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row.get("volume", 0)),
                    "oi": 0.0,
                }
            )
        return result
    except Exception as exc:
        log.info("bootstrap (no-OI) failed for %s (%s)", symbol, type(exc).__name__)
        return []


# ---------------------------------------------------------------------------
# SSE client handle
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The live-feed singleton
# ---------------------------------------------------------------------------


@dataclass
class LiveFeedService:
    """Manages a single KiteTicker connection for the dashboard.

    One instance is created at app startup and lives for the whole process.
    ``start()`` / ``stop()`` control whether ticks are streamed; the websocket
    thread keeps the Twisted reactor alive throughout.

    Parameters
    ----------
    ticker_factory:
        Callable that creates a KiteTicker-compatible object; defaults to the
        real SDK class.  Tests inject ``FakeTicker``.
    kite_factory:
        Callable that creates a KiteConnect-compatible object (for history).
    calendar:
        ``TradingCalendar`` for market-hours checks.  ``None`` → always treat
        market as open (simulator mode).
    is_simulated:
        ``True`` when started with ``--simulate-kite``; suppresses badge and
        keeps state permanently set to ``STATE_SIMULATED``.
    """

    ticker_factory: Callable[..., Any]
    kite_factory: Callable[[], Any]  # () → kite client with token set
    calendar: TradingCalendar | None = None
    is_simulated: bool = False
    max_subscriptions: int = MAX_SUBSCRIPTIONS
    clock: SystemClock = field(default_factory=SystemClock)

    # --- internal state (never serialised) ---
    _ticker: Any = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _state: str = field(default=STATE_DISCONNECTED, init=False)
    _reason: str = field(default="", init=False)
    _reconnect_count: int = field(default=0, init=False)
    _last_tick_time: float = field(default=0.0, init=False)  # time.time()
    _last_tick_exchange_ts: float = field(default=0.0, init=False)
    _last_tick_latency_ms: float | None = field(default=None, init=False)
    _subscribed_tokens: set[int] = field(default_factory=set, init=False)
    _futures_tokens: set[int] = field(default_factory=set, init=False)  # need FULL mode
    _registry: TokenRegistry = field(default_factory=TokenRegistry, init=False)
    _builder: CandleBuilder = field(default_factory=CandleBuilder, init=False)
    _sse_clients: list[SSEClient] = field(default_factory=list, init=False)
    _sse_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _last_push: dict[str, float] = field(default_factory=dict, init=False)  # symbol → time
    _stopped: bool = field(default=False, init=False)
    _bootstrap_data: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict, init=False
    )  # symbol → historical candles

    # --------------------------------------------------------------------- public
    def start(self, *, api_key: str, access_token: str) -> None:
        """Start (or reconnect) the ticker thread.

        Safe to call multiple times; subsequent calls while running are no-ops.
        """
        with self._lock:
            if self._state in (STATE_CONNECTING, STATE_LIVE):
                return
            if self._stopped:
                # Once the thread is dead we cannot restart in the same process
                # (Twisted reactor limitation).  Callers must handle this.
                log.warning("live feed: cannot restart after stop(); create a new instance")
                return
            self._state = STATE_CONNECTING
            self._reason = ""

        self._start_ticker(api_key=api_key, access_token=access_token)

    def stop(self) -> None:
        """Shut down the websocket and all SSE clients cleanly."""
        with self._lock:
            self._stopped = True
            self._state = STATE_DISCONNECTED

        ticker = self._ticker
        if ticker is not None:
            with contextlib.suppress(Exception):
                ticker.close()

        with self._sse_lock:
            for client in list(self._sse_clients):
                client.close()
            self._sse_clients.clear()

        log.info("live feed: stopped")

    def subscribe(
        self,
        symbols: list[tuple[str, int, bool]],
    ) -> None:
        """Subscribe to a list of (symbol, token, is_futures) tuples.

        Clips to ``max_subscriptions``.  Uses QUOTE mode by default; switches
        to FULL for futures/options (OI).
        """
        with self._lock:
            tokens_quote: list[int] = []
            tokens_full: list[int] = []
            for symbol, token, is_fut in symbols[: self.max_subscriptions]:
                self._registry.register(symbol, token)
                if token not in self._subscribed_tokens:
                    self._subscribed_tokens.add(token)
                    if is_fut:
                        self._futures_tokens.add(token)
                        tokens_full.append(token)
                    else:
                        tokens_quote.append(token)

        ticker = self._ticker
        if ticker is None:
            return
        try:
            all_new = tokens_quote + tokens_full
            if all_new:
                ticker.subscribe(all_new)
            if tokens_quote:
                ticker.set_mode("quote", tokens_quote)
            if tokens_full:
                ticker.set_mode("full", tokens_full)
        except Exception as exc:
            log.warning("live feed: subscribe failed (%s)", type(exc).__name__)

    def unsubscribe(self, tokens: list[int]) -> None:
        ticker = self._ticker
        with self._lock:
            for token in tokens:
                self._subscribed_tokens.discard(token)
                self._futures_tokens.discard(token)
        if ticker is not None:
            with contextlib.suppress(Exception):
                ticker.unsubscribe(tokens)

    def status(self) -> FeedStatus:
        """Current feed status for the UI status area and /api/data-sources."""
        now = time.time()
        with self._lock:
            state = self._state
            reason = self._reason
            reconnect_count = self._reconnect_count
            last_tick = self._last_tick_time
            latency_ms = self._last_tick_latency_ms
            subscribed = len(self._subscribed_tokens)

        if self.is_simulated and state not in (
            STATE_TOKEN_EXPIRED,
            STATE_DISCONNECTED,
        ):
            state = STATE_SIMULATED

        last_tick_age: float | None = None
        last_tick_time = ""
        if last_tick > 0:
            last_tick_age = now - last_tick
            last_tick_time = dt.datetime.fromtimestamp(last_tick, tz=IST).strftime("%H:%M:%S")
            # Stale check: if we have ticks but haven't seen one recently
            if state == STATE_LIVE and last_tick_age > _STALE_THRESHOLD:
                if self._market_is_open():
                    state = STATE_STALE
                    reason = f"No tick since {last_tick_time} IST"
                else:
                    state = STATE_MARKET_CLOSED
                    reason = "Market is closed"

        return FeedStatus(
            state=state,
            subscribed=subscribed,
            reconnect_count=reconnect_count,
            last_tick_age=last_tick_age,
            last_tick_time=last_tick_time,
            latency_ms=latency_ms,
            reason=reason,
        )

    def register_sse_client(self, client: SSEClient) -> None:
        with self._sse_lock:
            self._sse_clients.append(client)

    def unregister_sse_client(self, client: SSEClient) -> None:
        with self._sse_lock, contextlib.suppress(ValueError):
            self._sse_clients.remove(client)

    def set_token_expired(self) -> None:
        """Called when the ticker reports an auth failure mid-session."""
        with self._lock:
            self._state = STATE_TOKEN_EXPIRED
            self._reason = "Token expired. Reconnect on the Connect page."
        self._broadcast(
            SSEEvent(
                event="status",
                data={
                    "state": STATE_TOKEN_EXPIRED,
                    "reason": "Token expired. Reconnect on the Connect page.",
                    "fallback": "Switched to Delayed NSE.",
                },
            )
        )
        log.warning("live feed: token expired mid-session")

    def push_simulated_tick(self, tick: LiveTick) -> None:
        """Used by the simulator to inject ticks into the same pipeline."""
        self._handle_tick(tick)

    def bootstrap_data_for(self, symbol: str) -> list[dict[str, Any]]:
        """Return bootstrapped historical candles for a symbol (empty if unavailable)."""
        return self._bootstrap_data.get(symbol.upper(), [])

    def run_bootstrap(self, symbol: str, token: int) -> None:
        """Fetch today's history in a background thread and cache it."""

        def _do() -> None:
            kite = self._make_kite()
            if kite is None:
                return
            candles = _bootstrap_candles(kite, token, symbol)
            if not candles:
                candles = _bootstrap_without_oi(kite, token, symbol)
            if candles:
                self._bootstrap_data[symbol.upper()] = candles
                log.info("live feed: bootstrapped %d candles for %s", len(candles), symbol)
            else:
                log.info(
                    "live feed: no bootstrap candles for %s (plan may not include history)",
                    symbol,
                )

        t = threading.Thread(target=_do, name=f"kite-bootstrap-{symbol}", daemon=True)
        t.start()

    # ---------------------------------------------------------------- internal
    def _make_kite(self) -> Any | None:
        try:
            return self.kite_factory()
        except Exception as exc:
            log.warning("live feed: cannot create kite client (%s)", type(exc).__name__)
            return None

    def _start_ticker(self, *, api_key: str, access_token: str) -> None:
        try:
            ticker = self.ticker_factory(api_key, access_token)
        except Exception as exc:
            log.warning("live feed: could not create ticker (%s)", type(exc).__name__)
            with self._lock:
                self._state = STATE_DISCONNECTED
                self._reason = "Could not create the ticker."
            return

        ticker.on_ticks = self._on_ticks
        ticker.on_connect = self._on_connect
        ticker.on_close = self._on_close
        ticker.on_error = self._on_error
        ticker.on_reconnect = self._on_reconnect
        ticker.on_noreconnect = self._on_noreconnect
        self._ticker = ticker

        # Re-subscribe tokens that were requested before the ticker started
        with self._lock:
            tokens_quote = [t for t in self._subscribed_tokens if t not in self._futures_tokens]
            tokens_full = list(self._futures_tokens)

        def _connect() -> None:
            try:
                ticker.connect(threaded=True)
                if tokens_quote:
                    ticker.subscribe(tokens_quote)
                    ticker.set_mode("quote", tokens_quote)
                if tokens_full:
                    ticker.subscribe(tokens_full)
                    ticker.set_mode("full", tokens_full)
            except Exception as exc:
                log.warning("live feed: connect failed (%s)", type(exc).__name__)
                with self._lock:
                    self._state = STATE_DISCONNECTED
                    self._reason = "Connection failed."

        t = threading.Thread(target=_connect, name="kite-ticker-connect", daemon=True)
        t.start()
        self._thread = t

    # ---------------------------------------------------------------- callbacks
    def _on_connect(self, ws: Any, response: Any) -> None:
        log.info("live feed: connected")
        with self._lock:
            self._state = STATE_LIVE
            self._reason = ""
        self._broadcast(SSEEvent(event="status", data=self.status().as_dict()))

    def _on_ticks(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        received_at = time.time()
        for raw in ticks:
            self._process_raw_tick(raw, received_at)

    def _process_raw_tick(self, raw: dict[str, Any], received_at: float) -> None:
        token = int(raw.get("instrument_token", 0))
        symbol = self._registry.symbol_for(token)
        if symbol is None:
            return  # not subscribed by us

        price = float(raw.get("last_price", 0.0) or 0.0)
        volume_cum = raw.get("volume_traded")
        oi = float(raw.get("oi", 0.0) or 0.0)

        # Exchange timestamp: Kite sends a naive IST datetime
        exc_ts_raw = raw.get("exchange_timestamp") or raw.get("last_trade_time")
        if isinstance(exc_ts_raw, dt.datetime):
            exc_ts = float(to_ist_ts(exc_ts_raw).timestamp())
        else:
            exc_ts = received_at  # fallback; latency figure will be meaningless

        tick = LiveTick(
            symbol=symbol,
            token=token,
            price=price,
            volume_cum=int(volume_cum) if volume_cum is not None else None,
            oi=oi,
            exchange_ts=exc_ts,
            received_at=received_at,
            is_simulated=self.is_simulated,
        )
        self._handle_tick(tick)

    def _handle_tick(self, tick: LiveTick) -> None:
        """Process one normalised tick: update state, feed builder, broadcast SSE."""
        received_at = tick.received_at
        with self._lock:
            if (
                self._state not in (STATE_LIVE, STATE_STALE, STATE_SIMULATED)
                and not self.is_simulated
            ):
                return
            self._last_tick_time = received_at
            self._last_tick_exchange_ts = tick.exchange_ts
            # Latency
            diff_ms = (received_at - tick.exchange_ts) * 1000.0
            if 0 <= diff_ms <= 300_000:
                self._last_tick_latency_ms = diff_ms
            else:
                self._last_tick_latency_ms = None  # clock skew
            if self._state == STATE_STALE and not self.is_simulated:
                self._state = STATE_LIVE
                self._reason = ""

        # Feed the candle builder
        ist_ts = pd.Timestamp(received_at, unit="s", tz="UTC").tz_convert(IST)
        closed = self._builder.on_tick(
            tick.symbol,
            ist_ts,
            tick.price,
            volume_cum=tick.volume_cum,
            oi=tick.oi,
        )

        # Broadcast the raw tick (throttled per symbol)
        now_mono = time.monotonic()
        last = self._last_push.get(tick.symbol, 0.0)
        if now_mono - last >= _TICK_THROTTLE_PER_SYMBOL:
            self._last_push[tick.symbol] = now_mono
            tick_event = SSEEvent(
                event="tick",
                symbol=tick.symbol,
                data={
                    "symbol": tick.symbol,
                    "price": tick.price,
                    "oi": tick.oi,
                    "tick_time": dt.datetime.fromtimestamp(tick.exchange_ts, tz=IST).strftime(
                        "%H:%M:%S"
                    ),
                    "latency": tick.latency_note,
                    "latency_ms": tick.latency_ms,
                    "is_simulated": tick.is_simulated,
                },
            )
            self._broadcast_to_symbol(tick_event, tick.symbol)

            # Also broadcast the in-progress (forming) candle
            self._broadcast_forming(tick.symbol)

        # Broadcast any newly closed candles
        for candle in closed:
            self._broadcast_closed_candle(candle, tick.symbol, tick.is_simulated)

    def _broadcast_forming(self, symbol: str) -> None:
        """Push the current in-progress candle to subscribed SSE clients."""
        in_progress = self._builder.in_progress()
        minute = in_progress.get(symbol)
        if minute is None:
            return
        # We use the _open dict indirectly via pending()
        pending = self._builder.pending(pd.Timestamp.now(tz=IST) + pd.Timedelta(minutes=1))
        for candle in pending:
            if candle.symbol == symbol:
                self._broadcast_to_symbol(
                    SSEEvent(
                        event="forming",
                        symbol=symbol,
                        data=_candle_dict(candle, is_simulated=self.is_simulated),
                    ),
                    symbol,
                )

    def _broadcast_closed_candle(self, candle: Any, symbol: str, is_simulated: bool) -> None:
        self._broadcast_to_symbol(
            SSEEvent(
                event="candle",
                symbol=symbol,
                data=_candle_dict(candle, is_simulated=is_simulated),
            ),
            symbol,
        )

    def _broadcast(self, event: SSEEvent) -> None:
        """Broadcast to all SSE clients."""
        with self._sse_lock:
            for client in list(self._sse_clients):
                client.push(event)

    def _broadcast_to_symbol(self, event: SSEEvent, symbol: str) -> None:
        """Broadcast to SSE clients subscribed to this symbol."""
        sym = symbol.upper()
        with self._sse_lock:
            for client in list(self._sse_clients):
                if client.symbol == sym or client.symbol == "":
                    client.push(event)

    def _on_close(self, ws: Any, code: int, reason: str) -> None:
        with self._lock:
            if self._state == STATE_TOKEN_EXPIRED:
                return  # token_expired already set; do not overwrite
            self._state = STATE_DISCONNECTED
            self._reason = f"Disconnected (code {code})"
        log.info("live feed: closed code=%s reason=%s", code, reason)
        self._broadcast(SSEEvent(event="status", data=self.status().as_dict()))

    def _on_error(self, ws: Any, code: int, reason: str) -> None:
        log.warning("live feed: error code=%s reason=%s", code, reason)
        # Auth errors
        if code in (403, 401) or "token" in str(reason).lower():
            self.set_token_expired()
        else:
            with self._lock:
                self._state = STATE_DISCONNECTED
                self._reason = "Connection error."

    def _on_reconnect(self, ws: Any, attempts: int) -> None:
        with self._lock:
            self._reconnect_count += 1
            self._state = STATE_CONNECTING
            self._reason = f"Reconnecting (attempt {attempts})"
        log.info("live feed: reconnect attempt %d", attempts)
        self._broadcast(SSEEvent(event="status", data=self.status().as_dict()))

    def _on_noreconnect(self, ws: Any) -> None:
        with self._lock:
            self._state = STATE_DISCONNECTED
            self._reason = "Reconnect limit reached."
        log.warning("live feed: no reconnect; market may be closed")
        self._broadcast(SSEEvent(event="status", data=self.status().as_dict()))

    def _market_is_open(self) -> bool:
        if self.calendar is None:
            return True
        now = self.clock.now()
        from ..core.calendar import SessionWindow

        window: SessionWindow = self.calendar.window
        from ..core.clock import to_ist

        ist = to_ist(now)
        day = ist.date()
        if self.calendar.is_holiday(day) or self.calendar.is_weekend(day):
            return False
        return window.open_time <= ist.time() <= window.close_time


# ---------------------------------------------------------------------------
# SSE candle dict (chart-compatible format matching /api/chart/candles items)
# ---------------------------------------------------------------------------


def _candle_dict(candle: Any, *, is_simulated: bool) -> dict[str, Any]:
    """Convert a data.candle_builder.Candle → chart-compatible dict."""
    from .chart.transforms import ist_label

    # candle.time is a pd.Timestamp (from the builder)
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


# ---------------------------------------------------------------------------
# Global singleton (created once by the app factory)
# ---------------------------------------------------------------------------

_GLOBAL_FEED: LiveFeedService | None = None
_FEED_LOCK = threading.Lock()


def get_feed() -> LiveFeedService | None:
    return _GLOBAL_FEED


def init_feed(feed: LiveFeedService) -> None:
    global _GLOBAL_FEED
    with _FEED_LOCK:
        _GLOBAL_FEED = feed


__all__ = [
    "STATE_CONNECTING",
    "STATE_DISCONNECTED",
    "STATE_LIVE",
    "STATE_MARKET_CLOSED",
    "STATE_NO_PLAN",
    "STATE_SIMULATED",
    "STATE_STALE",
    "STATE_TOKEN_EXPIRED",
    "FeedStatus",
    "LiveFeedService",
    "LiveTick",
    "SSEClient",
    "SSEEvent",
    "TokenRegistry",
    "get_feed",
    "init_feed",
]
