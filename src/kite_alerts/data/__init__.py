"""Market-data adapters: instruments, history, candles, the websocket and the watchdog.

This package is the only place that talks to Kite's *market-data* endpoints. It
contains no order-side code path (``tests/test_alerts_only.py`` enforces that
mechanically), and every module is importable without a live session so the whole
pipeline can be exercised against ``tests/fakes/fake_kite.py``.
"""

from __future__ import annotations

from .candle_builder import CandleBuilder
from .feed import KiteFeed
from .history import HistoryClient, merge_frames, normalise_history
from .instruments import Instrument, InstrumentResolver
from .watchdog import FeedWatchdog, WatchdogActions

__all__ = [
    "CandleBuilder",
    "FeedWatchdog",
    "HistoryClient",
    "Instrument",
    "InstrumentResolver",
    "KiteFeed",
    "WatchdogActions",
    "merge_frames",
    "normalise_history",
]
