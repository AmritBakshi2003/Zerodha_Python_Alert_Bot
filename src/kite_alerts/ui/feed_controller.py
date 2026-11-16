"""Start and keep the Kite live feed alive for the dashboard (P0 / F1).

Before this module nothing ever called ``LiveFeedService.start()``, so the
ticker was never created: ``/api/data-sources`` advertised "live", the chart
source was selectable, and every live request returned an empty series.

``FeedController`` is the one place that turns "a page needs live data" into
"credentials + tokens + start + bootstrap". It is idempotent, so every caller
(app startup, login, chart symbol selection) can simply ask for it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TERMINAL_STATES = ("token_expired", "no_plan")


def default_resolver_factory(api_key: str, access_token: str, cache_dir: Path) -> Any:
    """Build the real resolver (imported lazily so tests never hit the network)."""
    from ..data.instruments import InstrumentResolver
    from .kite_connect import default_kite_factory

    return InstrumentResolver(default_kite_factory(api_key, access_token), cache_dir)


@dataclass
class FeedController:
    """Own the lifecycle of the shared feed: resolve, subscribe, start, bootstrap."""

    feed: Any
    settings_provider: Callable[[], Any]
    resolver_factory: Callable[[str, str, Path], Any] = default_resolver_factory
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _resolver: Any = field(default=None, init=False)
    _resolver_key: tuple[str, str] | None = field(default=None, init=False)
    _bootstrapped: set[str] = field(default_factory=set, init=False)

    def ensure_started(self, symbols: Sequence[str] | None = None) -> bool:
        """Start the feed (once) and subscribe ``symbols`` (default: settings).

        Returns True when the feed has what it needs to deliver live data.
        Never raises: a dashboard page must still render when Kite is down.
        """
        settings = self.settings_provider()
        if settings is None or self.feed is None:
            return False
        if getattr(self.feed, "is_simulated", False):
            return True  # the simulator owns the feed in this mode
        kite = settings.kite
        if not kite.api_key or not kite.token:
            return False

        try:
            with self._lock:
                resolver = self._resolver_for(settings, kite.api_key, kite.token)
                wanted = list(dict.fromkeys(symbols or settings.symbols))
                entries: list[tuple[str, int, bool]] = []
                for spec in wanted:
                    try:
                        instrument = resolver.resolve(spec)
                    except Exception as exc:
                        log.warning("live feed: cannot resolve %s (%s)", spec, type(exc).__name__)
                        continue
                    entries.append((instrument.symbol, instrument.token, instrument.is_future))

                if not entries:
                    return False

                self.feed.subscribe(entries)
                self.feed.start(api_key=kite.api_key, access_token=kite.token)

                for symbol, token, _ in entries:
                    if symbol in self._bootstrapped:
                        continue
                    self._bootstrapped.add(symbol)
                    self.feed.run_bootstrap(symbol, token)
        except Exception:
            log.warning("live feed: could not start the ticker", exc_info=True)
            return False

        state = self.feed.status().state
        return state not in TERMINAL_STATES

    def _resolver_for(self, settings: Any, api_key: str, access_token: str) -> Any:
        key = (api_key, access_token)
        if self._resolver is None or self._resolver_key != key:
            self._resolver = self.resolver_factory(api_key, access_token, Path(settings.cache_dir))
            self._resolver_key = key
        return self._resolver

    def reset(self) -> None:
        """Forget cached resolution after a settings/login change."""
        with self._lock:
            self._resolver = None
            self._resolver_key = None
            self._bootstrapped.clear()
