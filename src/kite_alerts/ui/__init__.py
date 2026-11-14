"""Optional local dashboard for kite-alerts (read-only history + demo player).

Importing this package must never import FastAPI: the headless bot and the
offline ``kite-alerts demo`` do not need the ``ui`` extra. The web application
lives in :mod:`kite_alerts.ui.app` and is imported only by
:mod:`kite_alerts.ui.server` and the ``kite-alerts ui`` subcommand.

ALERTS ONLY: nothing under this package places, modifies or cancels orders.
"""

from __future__ import annotations

__all__: list[str] = []
