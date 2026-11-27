"""Practice demo replay route."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..demo_player import build_demo_timeline_for
from ..security import require_csrf_token, require_same_origin
from .shared import NO_SETTINGS_MESSAGE, _error, _practice_payload

if TYPE_CHECKING:
    from ..app import UiContext


def practice_router(context: UiContext) -> APIRouter:
    """Build the router for the practice replay check."""
    router = APIRouter()

    @router.post("/api/practice/check")
    def practice_check(request: Request) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        if context.settings is None:
            return _error(503, NO_SETTINGS_MESSAGE, "no_settings")
        timeline = build_demo_timeline_for(context.settings.params)
        return JSONResponse(_practice_payload(timeline, context.version))

    return router
