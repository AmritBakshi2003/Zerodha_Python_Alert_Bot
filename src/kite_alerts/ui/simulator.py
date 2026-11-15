"""Kite market data simulator for testing without a Zerodha account (C4).

This module runs ONLY when the dashboard is explicitly started with
``--simulate-kite`` or ``SIMULATE_KITE=1``.  It makes no network calls,
requires no credentials, and generates synthetic ticks in the exact payload
shape that KiteTicker delivers.

Safety invariants
-----------------
* IMPOSSIBLE to mistake for real market data: permanent purple ``SIMULATED``
  badge, never the green ``LIVE`` badge, never counted as Kite connected.
* Completely excluded from alerts and the alert store.
* Unavailable unless ``--simulate-kite`` or ``SIMULATE_KITE=1`` is passed.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.clock import IST
from .chart.types import SymbolInfo
from .live_feed import LiveFeedService, LiveTick

log = logging.getLogger(__name__)

SIMULATED_EQUITY_TOKEN = 999901
SIMULATED_FUTURES_TOKEN = 999902

SIMULATED_EQUITY_SYMBOL = "NSE:SIMULATED"
SIMULATED_FUTURES_SYMBOL = "NFO:SIMFUT"

SIMULATOR_SYMBOLS: tuple[SymbolInfo, ...] = (
    SymbolInfo(
        symbol=SIMULATED_EQUITY_SYMBOL,
        label="SIMULATED (Equity)",
        provider_symbol=SIMULATED_EQUITY_SYMBOL,
        supported=True,
    ),
    SymbolInfo(
        symbol=SIMULATED_FUTURES_SYMBOL,
        label="SIMULATED FUT (OI)",
        provider_symbol=SIMULATED_FUTURES_SYMBOL,
        supported=True,
    ),
)


@dataclass
class SimulatorState:
    running: bool = False
    disconnected: bool = False
    token_expired: bool = False
    disconnect_until: float = 0.0
    tick_count: int = 0
    last_tick_time: float = 0.0
    equity_price: float = 2500.0
    equity_volume: int = 100_000
    futures_price: float = 2515.0
    futures_volume: int = 50_000
    futures_oi: float = 1_250_000.0


class KiteSimulator:
    """Generates synthetic KiteTicker-shaped ticks for testing without Zerodha."""

    def __init__(
        self,
        feed: LiveFeedService,
        *,
        tick_interval: float = 0.5,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.feed = feed
        self.tick_interval = tick_interval
        self._clock = clock
        self._lock = threading.Lock()
        self.state = SimulatorState()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._random = random.Random(42)  # reproducible seeded random

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        with self._lock:
            if self.state.running:
                return
            self.state.running = True
            self.state.token_expired = False
            self.state.disconnected = False
            self._stop_event.clear()

        # Register simulator symbols in feed token registry
        self.feed.subscribe(
            [
                (SIMULATED_EQUITY_SYMBOL, SIMULATED_EQUITY_TOKEN, False),
                (SIMULATED_FUTURES_SYMBOL, SIMULATED_FUTURES_TOKEN, True),
            ]
        )

        # Pre-seed bootstrap history for simulator symbols
        self._seed_bootstrap_history()

        t = threading.Thread(target=self._run_loop, name="kite-simulator", daemon=True)
        self._thread = t
        t.start()
        log.info(
            "kite simulator: started (symbols: %s, %s)",
            SIMULATED_EQUITY_SYMBOL,
            SIMULATED_FUTURES_SYMBOL,
        )

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            self.state.running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        log.info("kite simulator: stopped")

    # ----------------------------------------------------------- dev controls
    def trigger_disconnect(self, duration: float = 4.0) -> None:
        """Simulate an unexpected websocket drop followed by auto-reconnect."""
        with self._lock:
            self.state.disconnected = True
            self.state.disconnect_until = self._clock() + duration
        log.info("kite simulator: dev control - triggered disconnect for %.1fs", duration)
        # Notify feed of disconnect
        self.feed._on_close(None, 1006, "connection drop (simulated)")

    def trigger_token_expiry(self) -> None:
        """Simulate an authentication rejection mid-session."""
        with self._lock:
            self.state.token_expired = True
            self.state.running = False
        log.warning("kite simulator: dev control - triggered token expiry")
        self.feed.set_token_expired()

    def trigger_reconnect(self) -> None:
        """Resume normal simulation after a disconnect or expiry."""
        with self._lock:
            self.state.disconnected = False
            self.state.token_expired = False
            self.state.running = True
        log.info("kite simulator: dev control - reconnected")
        self.feed._on_connect(None, {})

    # --------------------------------------------------------------- loop
    def _run_loop(self) -> None:
        self.feed._on_connect(None, {})
        while not self._stop_event.is_set():
            now = self._clock()
            with self._lock:
                if self.state.token_expired:
                    break
                if self.state.disconnected:
                    if now >= self.state.disconnect_until:
                        self.state.disconnected = False
                        self.feed._on_reconnect(None, 1)
                        self.feed._on_connect(None, {})
                    else:
                        time.sleep(0.2)
                        continue

            # Emit ticks for both equity and futures
            self._emit_ticks()
            time.sleep(self.tick_interval)

    def _emit_ticks(self) -> None:
        now = self._clock()

        with self._lock:
            # Equity tick
            delta_eq = self._random.choice([-1.0, -0.5, 0.0, 0.5, 1.0])
            self.state.equity_price = round(max(10.0, self.state.equity_price + delta_eq), 2)
            vol_delta_eq = self._random.randint(50, 500)
            self.state.equity_volume += vol_delta_eq

            # Futures tick
            delta_fut = self._random.choice([-1.5, -0.5, 0.0, 0.5, 1.5])
            self.state.futures_price = round(max(10.0, self.state.futures_price + delta_fut), 2)
            vol_delta_fut = self._random.randint(100, 1000)
            self.state.futures_volume += vol_delta_fut
            oi_delta = self._random.choice([-500, 0, 500, 1000])
            self.state.futures_oi = max(100_000.0, self.state.futures_oi + oi_delta)

            self.state.tick_count += 2
            self.state.last_tick_time = now

            eq_price = self.state.equity_price
            eq_vol = self.state.equity_volume
            fut_price = self.state.futures_price
            fut_vol = self.state.futures_volume
            fut_oi = self.state.futures_oi

        # Deliver to feed pipeline
        eq_tick = LiveTick(
            symbol=SIMULATED_EQUITY_SYMBOL,
            token=SIMULATED_EQUITY_TOKEN,
            price=eq_price,
            volume_cum=eq_vol,
            oi=0.0,
            exchange_ts=now - 0.035,  # 35ms simulated latency
            received_at=now,
            is_simulated=True,
        )
        fut_tick = LiveTick(
            symbol=SIMULATED_FUTURES_SYMBOL,
            token=SIMULATED_FUTURES_TOKEN,
            price=fut_price,
            volume_cum=fut_vol,
            oi=fut_oi,
            exchange_ts=now - 0.040,  # 40ms simulated latency
            received_at=now,
            is_simulated=True,
        )
        self.feed.push_simulated_tick(eq_tick)
        self.feed.push_simulated_tick(fut_tick)

    # ------------------------------------------------------------- bootstrap
    def _seed_bootstrap_history(self) -> None:
        """Pre-populate 120 minutes of 1m synthetic history for simulator symbols."""
        now = self._clock()
        base_epoch = int(now) - (120 * 60)
        # Floor to minute
        base_epoch = (base_epoch // 60) * 60

        eq_candles: list[dict[str, Any]] = []
        fut_candles: list[dict[str, Any]] = []
        eq_p = 2480.0
        fut_p = 2495.0
        fut_oi = 1_200_000.0

        for i in range(120):
            m_time = base_epoch + (i * 60)
            m_dt = dt.datetime.fromtimestamp(m_time, tz=IST).strftime("%Y-%m-%d %H:%M")

            # Equity candle
            open_eq = eq_p
            high_eq = round(open_eq + abs(self._random.gauss(0, 1.5)), 2)
            low_eq = round(open_eq - abs(self._random.gauss(0, 1.5)), 2)
            close_eq = round(self._random.uniform(low_eq, high_eq), 2)
            eq_p = close_eq
            eq_candles.append(
                {
                    "time": m_time,
                    "ist": m_dt,
                    "open": open_eq,
                    "high": high_eq,
                    "low": low_eq,
                    "close": close_eq,
                    "volume": self._random.randint(2000, 15000),
                    "oi": 0.0,
                    "is_simulated": True,
                }
            )

            # Futures candle
            open_fut = fut_p
            high_fut = round(open_fut + abs(self._random.gauss(0, 2.0)), 2)
            low_fut = round(open_fut - abs(self._random.gauss(0, 2.0)), 2)
            close_fut = round(self._random.uniform(low_fut, high_fut), 2)
            fut_p = close_fut
            fut_oi += self._random.randint(-1000, 2000)
            fut_candles.append(
                {
                    "time": m_time,
                    "ist": m_dt,
                    "open": open_fut,
                    "high": high_fut,
                    "low": low_fut,
                    "close": close_fut,
                    "volume": self._random.randint(5000, 30000),
                    "oi": fut_oi,
                    "is_simulated": True,
                }
            )

        self.feed._bootstrap_data[SIMULATED_EQUITY_SYMBOL] = eq_candles
        self.feed._bootstrap_data[SIMULATED_FUTURES_SYMBOL] = fut_candles


__all__ = [
    "SIMULATED_EQUITY_SYMBOL",
    "SIMULATED_EQUITY_TOKEN",
    "SIMULATED_FUTURES_SYMBOL",
    "SIMULATED_FUTURES_TOKEN",
    "SIMULATOR_SYMBOLS",
    "KiteSimulator",
    "SimulatorState",
]
