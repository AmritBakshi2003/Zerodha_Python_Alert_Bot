"""kite-alerts: Zerodha Kite Connect trade-ALERT bot.

ALERTS ONLY. This package contains no order-entry code path: it reads market
data and sends notifications. ``tests/test_alerts_only.py`` enforces that
mechanically against the whole source tree.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
