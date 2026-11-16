"""Provider adapters behind the one small ``ChartSource`` interface.

A source returns normalised candles only. It must not touch the alert engine,
the notifier or the alert store, and it must never accept a user-supplied URL.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import pandas as pd
import requests

from ...config.settings import Settings
from ...core.clock import IST, to_ist_ts
from ...demo import synthetic_day
from .resilience import ProviderCache
from .symbols import (
    DEMO_SYMBOL,
    crypto_symbols,
    delayed_symbols,
    lookup_nse,
)
from .transforms import from_frame, resample_candles
from .types import Candle, RawSeries, SourceError, SourceSeries, SymbolInfo

log = logging.getLogger(__name__)

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_PAIRS: dict[str, str] = {"BTC": "XBTUSD", "ETH": "ETHUSD"}
INTERVALS: tuple[int, ...] = (1, 3, 5, 15)


class ChartSource(Protocol):
    """The only interface the service and the routes know.

    ``id`` is stable and used by the picker; ``badge`` is the mandatory honesty
    label; ``session_based`` is false for 24/7 crypto so the NSE opening-range
    rule is not pretended to apply. ``fetch`` raises :class:`SourceError` with a
    plain message and never leaks a URL or a provider body.
    """

    id: str
    label: str
    badge: str
    supports_oi: bool
    enabled: bool
    session_based: bool

    def symbols(self, settings: Settings | None) -> tuple[SymbolInfo, ...]: ...

    def fetch(self, symbol: str, interval: int, settings: Settings | None) -> SourceSeries: ...


def _normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    renamed = {str(column).lower(): column for column in frame.columns}
    if not {"open", "high", "low", "close"} <= set(renamed):
        return pd.DataFrame()
    result = pd.DataFrame(
        {
            "open": frame[renamed["open"]],
            "high": frame[renamed["high"]],
            "low": frame[renamed["low"]],
            "close": frame[renamed["close"]],
        }
    )
    result["volume"] = frame[renamed["volume"]] if "volume" in renamed else 0.0
    result.index = frame.index
    return result.dropna(subset=["open", "high", "low", "close"])


def yfinance_history(provider_symbol: str, interval: int) -> pd.DataFrame:
    """Default delayed-NSE fetch. Imported lazily so the bot never needs yfinance."""
    import yfinance as yf

    period = "5d" if interval == 1 else "1mo"
    frame = yf.Ticker(provider_symbol).history(
        period=period,
        interval=f"{interval}m",
        auto_adjust=False,
        actions=False,
    )
    return _normalise_frame(frame)


def _delay_seconds(candles: tuple[Candle, ...], interval: int, now: float) -> float | None:
    if not candles:
        return None
    return max(0.0, now - (candles[-1].time + interval * 60))


@dataclass
class DelayedNSESource:
    id: str = "nse_delayed"
    label: str = "Delayed NSE"
    badge: str = "DELAYED"
    supports_oi: bool = False
    enabled: bool = True
    session_based: bool = True
    history: Callable[[str, int], pd.DataFrame] = yfinance_history
    cache: ProviderCache = field(default_factory=lambda: ProviderCache(ttl=8.0))
    now: Callable[[], float] = lambda: time.time()

    def symbols(self, settings: Settings | None) -> tuple[SymbolInfo, ...]:
        configured = settings.symbols if settings is not None else ()
        return delayed_symbols(configured)

    def fetch(self, symbol: str, interval: int, settings: Settings | None) -> SourceSeries:
        configured = settings.symbols if settings is not None else ()
        info = lookup_nse(configured, symbol)
        if not info.supported:
            raise SourceError(info.reason, code="unsupported_symbol")
        provider_interval = 1 if interval in (1, 3) else interval
        key = f"nse:{info.provider_symbol}:{provider_interval}"

        def load() -> RawSeries:
            try:
                frame = self.history(info.provider_symbol, provider_interval)
            except Exception as exc:
                log.warning("delayed NSE fetch failed (%s)", type(exc).__name__)
                raise SourceError(
                    "Delayed NSE data is temporarily unavailable. Retrying shortly."
                ) from None
            candles = from_frame(frame, provider_interval=provider_interval)
            if not candles:
                raise SourceError(
                    f"Delayed NSE returned no candles for {info.label}. The symbol may be "
                    "invalid, or the market may not have traded yet."
                )
            return RawSeries(
                candles=candles,
                provider_interval=provider_interval,
                supports_oi=False,
                delay_seconds=_delay_seconds(candles, provider_interval, self.now()),
            )

        result = self.cache.get(key, load)
        raw: RawSeries = result.value
        candles = resample_candles(raw.candles, interval) if interval == 3 else raw.candles
        return SourceSeries(
            candles=candles,
            provider_interval=raw.provider_interval,
            supports_oi=False,
            delay_seconds=raw.delay_seconds,
            stale=result.stale,
            stale_since=int(self.now() - result.age_seconds) if result.stale else None,
            retry_after=result.retry_after,
            message=result.error.message if result.error else "",
        )


@dataclass
class CryptoShowcaseSource:
    id: str = "crypto_showcase"
    label: str = "Crypto showcase"
    badge: str = "SHOWCASE"
    supports_oi: bool = False
    enabled: bool = True
    session_based: bool = False
    http_get: Callable[..., Any] = requests.get
    cache: ProviderCache = field(default_factory=lambda: ProviderCache(ttl=1.5))
    now: Callable[[], float] = lambda: time.time()
    timeout: float = 8.0

    def symbols(self, settings: Settings | None) -> tuple[SymbolInfo, ...]:
        return crypto_symbols()

    def _pair(self, symbol: str) -> str:
        normalized = str(symbol).strip().upper()
        if normalized not in KRAKEN_PAIRS:
            raise SourceError(
                "The crypto showcase supports BTC and ETH only.",
                code="unsupported_symbol",
            )
        return KRAKEN_PAIRS[normalized]

    def fetch(self, symbol: str, interval: int, settings: Settings | None) -> SourceSeries:
        pair = self._pair(symbol)
        provider_interval = 1 if interval in (1, 3) else interval
        key = f"kraken:{pair}:{provider_interval}"

        def load() -> RawSeries:
            try:
                response = self.http_get(
                    KRAKEN_OHLC_URL,
                    params={"pair": pair, "interval": provider_interval},
                    timeout=self.timeout,
                    headers={"User-Agent": "kite-alerts/1.0 (local dashboard)"},
                )
            except Exception as exc:
                log.warning("crypto provider request failed (%s)", type(exc).__name__)
                raise SourceError(
                    "The crypto showcase is temporarily unavailable. Delayed NSE still works."
                ) from None
            status = int(getattr(response, "status_code", 200) or 200)
            if status == 429:
                raise SourceError(
                    "The crypto showcase is rate limited right now. Retrying shortly.",
                    retry_after=30.0,
                    code="rate_limited",
                )
            if status >= 400:
                raise SourceError(
                    "The crypto showcase is temporarily unavailable. Retrying shortly."
                )
            try:
                payload = response.json()
            except ValueError:
                raise SourceError(
                    "The crypto showcase sent an unreadable response. Retrying shortly."
                ) from None
            if not isinstance(payload, dict) or payload.get("error"):
                raise SourceError(
                    "The crypto showcase is temporarily unavailable. Retrying shortly."
                )
            result = payload.get("result")
            if not isinstance(result, dict):
                raise SourceError("The crypto showcase sent no candles.")
            rows = result.get(pair)
            if not isinstance(rows, list):
                rows = next(
                    (
                        value
                        for key, value in result.items()
                        if key != "last" and isinstance(value, list)
                    ),
                    None,
                )
            candles = self._parse_rows(rows if isinstance(rows, list) else [])
            if not candles:
                raise SourceError("The crypto showcase returned no usable candles.")
            return RawSeries(
                candles=candles,
                provider_interval=provider_interval,
                supports_oi=False,
                delay_seconds=_delay_seconds(candles, provider_interval, self.now()),
            )

        result = self.cache.get(key, load, ttl=1.0 if provider_interval == 1 else 2.0)
        raw: RawSeries = result.value
        candles = resample_candles(raw.candles, interval) if interval == 3 else raw.candles
        return SourceSeries(
            candles=candles,
            provider_interval=raw.provider_interval,
            supports_oi=False,
            delay_seconds=raw.delay_seconds,
            stale=result.stale,
            stale_since=int(self.now() - result.age_seconds) if result.stale else None,
            retry_after=result.retry_after,
            message=result.error.message if result.error else "",
        )

    @staticmethod
    def _parse_rows(rows: list[Any]) -> tuple[Candle, ...]:
        candles: list[Candle] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                continue
            try:
                candles.append(
                    Candle(
                        time=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[6]),
                    )
                )
            except (TypeError, ValueError):
                continue
        candles.sort(key=lambda candle: candle.time)
        return tuple(candles)


@dataclass
class DemoSource:
    id: str = "demo"
    label: str = "Demo replay"
    badge: str = "DEMO"
    supports_oi: bool = True
    enabled: bool = True
    session_based: bool = True

    def symbols(self, settings: Settings | None) -> tuple[SymbolInfo, ...]:
        return (DEMO_SYMBOL,)

    def fetch(self, symbol: str, interval: int, settings: Settings | None) -> SourceSeries:
        if str(symbol).strip().upper() != DEMO_SYMBOL.symbol:
            raise SourceError(
                "The demo replay has one synthetic symbol: DEMO:FUT.",
                code="unsupported_symbol",
            )
        frame = synthetic_day()
        if frame.empty:
            raise SourceError("The demo data could not be built.")
        last_day = frame.index.date[-1]
        session = frame[frame.index.date == last_day]
        candles = from_frame(session, provider_interval=1)
        if interval != 1:
            candles = resample_candles(candles, interval)
        return SourceSeries(
            candles=candles,
            provider_interval=1,
            supports_oi=True,
        )


@dataclass
class KiteLiveSource:
    id: str = "kite_live"
    label: str = "Kite live"
    supports_oi: bool = True
    session_based: bool = True
    feed_provider: Callable[[], Any] | None = None
    _enabled: bool | None = None
    _badge: str | None = None

    @property
    def enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        try:
            from ..live_feed import STATE_DISCONNECTED, STATE_NO_PLAN, STATE_TOKEN_EXPIRED

            feed = self.feed_provider() if self.feed_provider else None
            if feed is None:
                return False
            availability = getattr(feed, "availability", None)
            if availability is not None:
                # One answer for every surface (F5).
                return bool(availability().enabled)
            st = feed.status().state
            return st not in (STATE_DISCONNECTED, STATE_NO_PLAN, STATE_TOKEN_EXPIRED)
        except Exception:
            return False

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def notice(self) -> str:
        """The sentence every surface shows for this source, or an empty string."""
        try:
            feed = self.feed_provider() if self.feed_provider else None
            if feed is None:
                return "Connect to Zerodha to use live Kite market data."
            availability = getattr(feed, "availability", None)
            if availability is not None:
                return str(availability().notice)
        except Exception:
            return ""
        return ""

    @property
    def badge(self) -> str:
        if self._badge is not None:
            return self._badge
        try:
            feed = self.feed_provider() if self.feed_provider else None
            if feed is not None and getattr(feed, "is_simulated", False):
                return "SIMULATED"
        except Exception:
            pass
        return "LIVE"

    @badge.setter
    def badge(self, value: str) -> None:
        self._badge = value

    def symbols(self, settings: Settings | None) -> tuple[SymbolInfo, ...]:
        try:
            feed = self.feed_provider() if self.feed_provider else None
            if feed is not None and getattr(feed, "is_simulated", False):
                from ..simulator import SIMULATOR_SYMBOLS

                return SIMULATOR_SYMBOLS
        except Exception:
            pass
        configured = settings.symbols if settings is not None else ()
        if not configured:
            from .symbols import DEFAULT_NSE_SYMBOLS

            configured = DEFAULT_NSE_SYMBOLS
        res: list[SymbolInfo] = []
        for s in configured:
            sym = str(s).strip().upper()
            label = sym.split(":", 1)[1] if ":" in sym else sym
            res.append(SymbolInfo(sym, label, sym))
        return tuple(res)

    def fetch(self, symbol: str, interval: int, settings: Settings | None) -> SourceSeries:
        feed = self.feed_provider() if self.feed_provider else None
        if feed is None:
            raise SourceError(
                "Live Kite data is not available here. The delayed NSE source works now.",
                code="feed_unavailable",
            )
        if not self.enabled:
            reason = "Live Kite feed is not currently connected."
            availability = getattr(feed, "availability", None)
            if availability is not None:
                reason = availability().notice
            else:
                st = feed.status()
                if st.reason:
                    reason = st.reason
            raise SourceError(reason, code="feed_unavailable")

        raw_list = feed.bootstrap_data_for(symbol)
        candles: list[Candle] = []
        for item in raw_list:
            if isinstance(item, dict):
                candles.append(
                    Candle(
                        time=int(item["time"]),
                        open=float(item["open"]),
                        high=float(item["high"]),
                        low=float(item["low"]),
                        close=float(item["close"]),
                        volume=float(item.get("volume", 0.0)),
                        oi=float(item["oi"]) if item.get("oi") is not None else None,
                    )
                )

        builder = getattr(feed, "_builder", None)
        if builder is not None:
            try:
                now_ist = pd.Timestamp.now(tz=IST)
                pending_candles = builder.pending(now_ist + pd.Timedelta(minutes=1))
                seen_times = {c.time for c in candles}
                for pc in pending_candles:
                    if pc.symbol.upper() == symbol.upper():
                        epoch = int(to_ist_ts(pc.time).timestamp())
                        if epoch not in seen_times:
                            candles.append(
                                Candle(
                                    time=epoch,
                                    open=float(pc.open),
                                    high=float(pc.high),
                                    low=float(pc.low),
                                    close=float(pc.close),
                                    volume=float(pc.volume),
                                    oi=float(pc.oi) if pc.oi else None,
                                )
                            )
                            seen_times.add(epoch)
            except Exception:
                pass

        candles.sort(key=lambda c: c.time)
        if not candles:
            raise SourceError(
                f"Waiting for live ticks for {symbol}...",
                code="waiting_for_ticks",
                retry_after=2.0,
            )

        c_tuple = tuple(candles)
        if interval != 1:
            c_tuple = resample_candles(c_tuple, interval)

        has_oi = any(c.oi is not None and c.oi > 0 for c in c_tuple)
        st = feed.status()
        return SourceSeries(
            candles=c_tuple,
            provider_interval=1,
            supports_oi=has_oi,
            delay_seconds=0.0,
            stale=(st.state == "stale"),
            message=st.reason if st.state == "stale" else "",
        )


def default_sources() -> dict[str, ChartSource]:
    return {
        "nse_delayed": DelayedNSESource(),
        "crypto_showcase": CryptoShowcaseSource(),
        "demo": DemoSource(),
        "kite_live": KiteLiveSource(),
    }


__all__ = [
    "INTERVALS",
    "KRAKEN_OHLC_URL",
    "KRAKEN_PAIRS",
    "ChartSource",
    "CryptoShowcaseSource",
    "DelayedNSESource",
    "DemoSource",
    "KiteLiveSource",
    "default_sources",
    "yfinance_history",
]
