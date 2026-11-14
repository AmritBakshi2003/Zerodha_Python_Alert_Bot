"""Kite websocket adapter.

The only job of this module is to turn raw Kite ticks into :class:`Tick` objects
and hand them to ``sink``. It runs on Kite's SDK thread, so:

* every tick is handled inside ``try/except`` - an unhandled exception here would
  silently kill the socket thread;
* the sink only enqueues, so a slow or broken consumer can never block the socket;
* subscriptions are batched, because Kite rejects a single call carrying thousands
  of tokens;
* an expired token is reported through ``on_auth_error`` (which stops the run with
  one alert) instead of being retried forever;
* a tick whose own timestamp is ancient is re-stamped with the clock, so a stale
  ``last_trade_time`` cannot replay a price into a long-closed minute.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

from ..config.settings import ResilienceSettings
from ..core.clock import Clock, SystemClock, to_ist_ts
from ..core.models import Tick
from ..core.ports import MetricsPort, metric

log = logging.getLogger(__name__)

#: Kite accepts at most a few thousand tokens per subscribe call; batch well below.
SUBSCRIBE_BATCH = 200

#: A tick timestamp older than this is treated as stale and replaced by the clock.
STALE_TICK_SECONDS = 120.0

#: Websocket close/error codes that mean "the session is not authorised".
AUTH_CODES = frozenset({401, 403})

_AUTH_MARKERS = ("tokenexception", "token", "forbidden", "unauthorised", "unauthorized")


class KiteFeed:
    """Thin, defensive wrapper around ``KiteTicker``."""

    def __init__(
        self,
        *,
        api_key: str,
        access_token: str,
        token_map: Mapping[str, int],
        sink: Callable[[Tick], None],
        settings: ResilienceSettings | None = None,
        metrics: MetricsPort | None = None,
        clock: Clock | None = None,
        ticker_factory: Callable[..., Any] | None = None,
        on_connect_hook: Callable[[bool], None] | None = None,
        on_auth_error: Callable[[str], None] | None = None,
    ) -> None:
        self._api_key = api_key
        self._access_token = access_token
        self._tok2sym = {int(token): symbol for symbol, token in token_map.items()}
        self._sink = sink
        self._settings = settings or ResilienceSettings()
        self._metrics = metrics
        self._clock: Clock = clock or SystemClock()
        self._ticker_factory = ticker_factory
        self._on_connect_hook = on_connect_hook
        self._on_auth_error = on_auth_error
        self._ticker: Any | None = None
        self._first_connect = True
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ state
    @property
    def token_map(self) -> dict[int, str]:
        """``instrument_token -> symbol`` for everything subscribed."""
        return dict(self._tok2sym)

    @property
    def connected(self) -> bool:
        ticker = self._ticker
        return bool(ticker is not None and getattr(ticker, "connected", False))

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Create the socket and connect (threaded). Idempotent while connected."""
        with self._lock:
            if self._ticker is not None:
                return
            ticker = self._build_ticker()
            self._ticker = ticker
            self._wire(ticker)
        log.info("connecting to KiteTicker (%d instrument(s))", len(self._tok2sym))
        ticker.connect(threaded=True)

    def stop(self) -> None:
        """Stop and close the socket. Safe to call twice, and before ``start``."""
        with self._lock:
            ticker, self._ticker = self._ticker, None
        if ticker is None:
            return
        for method in ("stop", "close"):
            try:
                getattr(ticker, method)()
            except Exception:  # shutdown must not raise
                log.debug("ticker.%s() failed", method, exc_info=True)
        metric(self._metrics, "ws_disconnects")
        log.info("websocket stopped")

    # ------------------------------------------------------------------ SDK wiring
    def _build_ticker(self) -> Any:
        if self._ticker_factory is not None:
            return self._ticker_factory(self._api_key, self._access_token)
        from kiteconnect import KiteTicker

        return KiteTicker(
            self._api_key,
            self._access_token,
            reconnect=True,
            reconnect_max_tries=self._settings.reconnect_max_tries,
            reconnect_max_delay=self._settings.reconnect_max_delay,
        )

    def _wire(self, ticker: Any) -> None:
        for name, callback in (
            ("on_ticks", self._on_ticks),
            ("on_connect", self._on_connect),
            ("on_close", self._on_close),
            ("on_error", self._on_error),
            ("on_reconnect", self._on_reconnect),
            ("on_noreconnect", self._on_noreconnect),
        ):
            try:
                setattr(ticker, name, callback)
            except (AttributeError, TypeError):  # pragma: no cover - immutable stand-in
                log.debug("ticker does not accept %s", name)

    def _on_connect(self, ticker: Any, _response: Any = None) -> None:
        tokens = list(self._tok2sym)
        first = self._first_connect
        self._first_connect = False
        metric(self._metrics, "ws_connects")
        self._subscribe(ticker, tokens)
        log.info("websocket connected; subscribed to %d instrument(s)", len(tokens))
        if self._on_connect_hook is not None:
            try:
                self._on_connect_hook(first)
            except Exception:  # a hook bug must not kill the socket
                log.exception("websocket connect hook failed")

    def _subscribe(self, ticker: Any, tokens: Sequence[int]) -> None:
        mode = getattr(ticker, "MODE_FULL", "full")
        for start in range(0, len(tokens), SUBSCRIBE_BATCH):
            batch = list(tokens[start : start + SUBSCRIBE_BATCH])
            ticker.subscribe(batch)
            ticker.set_mode(mode, batch)

    # ------------------------------------------------------------------ ticks
    def _on_ticks(self, _ticker: Any, ticks: Sequence[Mapping[str, Any]]) -> None:
        for raw in ticks:
            try:
                self._handle_tick(raw)
            except Exception:  # never let a tick kill the socket thread
                metric(self._metrics, "tick_errors")
                log.exception("tick handling error")

    def _handle_tick(self, raw: Mapping[str, Any]) -> None:
        token = int(raw["instrument_token"])
        symbol = self._tok2sym.get(token)
        if symbol is None:
            metric(self._metrics, "unknown_tokens", 1, token=str(token))
            return
        if "last_price" not in raw:
            metric(self._metrics, "ticks_without_price")
            return
        price = float(raw["last_price"])
        if price <= 0.0:
            metric(self._metrics, "invalid_ticks")
            return
        volume = raw.get("volume_traded")
        tick = Tick(
            symbol=symbol,
            token=token,
            ts=self._timestamp(raw),
            price=price,
            volume_cum=None if volume is None else int(volume),
            oi=float(raw.get("oi") or 0.0),
        )
        metric(self._metrics, "ticks_received")
        self._sink(tick)

    def _timestamp(self, raw: Mapping[str, Any]) -> pd.Timestamp:
        """Event time for a tick, defending against a stale exchange timestamp."""
        now = to_ist_ts(self._clock.now())
        for key in ("exchange_timestamp", "last_trade_time"):
            value = raw.get(key)
            if value is None:
                continue
            stamp = to_ist_ts(value)
            if now - stamp > pd.Timedelta(seconds=STALE_TICK_SECONDS):
                metric(self._metrics, "stale_tick_timestamps")
                log.debug(
                    "stale %s (%s) for token %s; using the clock",
                    key,
                    stamp,
                    raw.get("instrument_token"),
                )
                return now
            return stamp
        return now

    # ------------------------------------------------------------------ failures
    def _on_close(self, _ticker: Any, code: Any, reason: Any) -> None:
        metric(self._metrics, "ws_closes")
        text = f"{code} {reason}".strip()
        if _looks_like_auth(code, text):
            self._report_auth(
                f"Kite closed the websocket with an authorisation error ({text}). "
                "The access token expires daily: run `kite-alerts login` and restart."
            )
        else:
            log.warning("websocket closed: %s", text)

    def _on_error(self, _ticker: Any, code: Any, reason: Any) -> None:
        metric(self._metrics, "ws_errors")
        text = f"{code} {reason}".strip()
        if _looks_like_auth(code, text):
            self._report_auth(
                f"Kite rejected the websocket session ({text}). "
                "The access token expires daily: run `kite-alerts login` and restart."
            )
        else:
            log.error("websocket error: %s", text)

    def _on_reconnect(self, _ticker: Any, attempts: Any = 0) -> None:
        metric(self._metrics, "ws_reconnects")
        log.warning("websocket reconnecting (attempt %s)", attempts)

    def _on_noreconnect(self, _ticker: Any) -> None:
        metric(self._metrics, "ws_noreconnect")
        log.error("websocket gave up reconnecting; an operator restart is required")

    def _report_auth(self, message: str) -> None:
        log.error("%s", message)
        metric(self._metrics, "auth_errors")
        if self._on_auth_error is None:
            return
        try:
            self._on_auth_error(message)
        except Exception:
            log.exception("auth error hook failed")


def _looks_like_auth(code: Any, text: str) -> bool:
    try:
        if int(code) in AUTH_CODES:
            return True
    except (TypeError, ValueError):
        pass
    lowered = text.lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)
