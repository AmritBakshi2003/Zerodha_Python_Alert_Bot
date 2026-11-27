"""Telegram setup route."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..security import require_csrf_token, require_same_origin

if TYPE_CHECKING:
    from ..app import UiContext


def notifications_router(context: UiContext) -> APIRouter:
    """Build the router for the notifier setup surface."""
    router = APIRouter()

    @router.post("/api/notifications/telegram/chat-id")
    def telegram_chat_id(request: Request) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        assert context.telegram is not None
        return JSONResponse(context.telegram.find_chat_id().as_dict())

    return router
