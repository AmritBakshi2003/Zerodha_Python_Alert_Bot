"""The read-only Chart workstation backend (C3).

One interface (:class:`~kite_alerts.ui.chart.sources.ChartSource`) returns
normalised candles. Adapters never touch alerts, orders or the browser.
"""

from .service import ChartService, build_signal, indicator_frame
from .types import Candle, ChartError, SourceError, SourceInfo, SymbolInfo

__all__ = [
    "Candle",
    "ChartError",
    "ChartService",
    "SourceError",
    "SourceInfo",
    "SymbolInfo",
    "build_signal",
    "indicator_frame",
]
