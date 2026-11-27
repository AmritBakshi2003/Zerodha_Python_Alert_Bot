"""Router factories for the dashboard FastAPI app."""

from __future__ import annotations

from .alerts import alerts_router
from .chart import chart_router
from .dashboard import dashboard_router
from .kite import kite_router
from .notifications import notifications_router
from .overview import overview_router
from .practice import practice_router
from .settings import settings_router

__all__ = [
    "alerts_router",
    "chart_router",
    "dashboard_router",
    "kite_router",
    "notifications_router",
    "overview_router",
    "practice_router",
    "settings_router",
]
