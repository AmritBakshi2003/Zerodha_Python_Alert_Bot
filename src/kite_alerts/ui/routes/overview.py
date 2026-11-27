"""Read-only dashboard routes: about, status and the alert-history feed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response

from ..security import resolve_csrf, set_csrf_cookie
from .shared import (
    ALERTS_ONLY_STATEMENT,
    ATTRIBUTION,
    FIRST_DAY_CHECKLIST,
    MODE,
    _resolved_paths,
)

if TYPE_CHECKING:
    from ..app import UiContext


def overview_router(context: UiContext) -> APIRouter:
    """Build the router for /api/about, /api/about.paths and /api/status."""
    router = APIRouter()

    @router.get("/api/about")
    def about(request: Request, response: Response) -> dict[str, Any]:
        holidays = context.chart.holiday_summary() if context.chart is not None else None
        settings = context.settings
        token, issued = resolve_csrf(request)
        if issued:
            set_csrf_cookie(response, token)
        payload: dict[str, Any] = {
            "version": context.version,
            "mode": MODE,
            "alerts_only": True,
            "alerts_only_statement": ALERTS_ONLY_STATEMENT,
            "checklist": list(FIRST_DAY_CHECKLIST),
            "attribution": dict(ATTRIBUTION),
            "settings_error": context.settings_error,
            "kite_configured": bool(settings and settings.kite.api_key),
            "token_present": bool(settings and settings.kite.token),
            "holidays": holidays,
            "simulate_kite": context.simulate_kite,
            "demo": True,
            "redirect_url": context.kite.redirect_url if context.kite else "",
            "csrf_token": token,
            "paths": _resolved_paths(context),
        }
        return payload

    @router.get("/api/about.paths")
    def about_paths() -> dict[str, Any]:
        return _resolved_paths(context)

    @router.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "status": context.status.snapshot().as_dict(),
            "today": context.history.today_counts(),
            "version": context.version,
            "mode": MODE,
            "alerts_only": True,
            "settings_error": context.settings_error,
            "warnings": context.settings.warnings_list() if context.settings else [],
        }

    return router
