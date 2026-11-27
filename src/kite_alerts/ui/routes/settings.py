"""Settings read/validate/write routes."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..security import require_csrf_token, require_same_origin
from ..settings_schema import connect_payload, settings_payload
from .shared import SettingsUpdate

if TYPE_CHECKING:
    from ..app import UiContext


def settings_router(context: UiContext) -> APIRouter:
    """Build the router for the settings surface (read, validate, write)."""
    router = APIRouter()

    @router.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        assert context.settings_store is not None
        return settings_payload(
            context.settings,
            settings_error=context.settings_error,
            env_file=str(context.settings_store.path),
            connect=connect_payload(context.settings, settings_error=context.settings_error),
        )

    @router.put("/api/settings")
    def put_settings(request: Request, update: SettingsUpdate) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        assert context.settings_store is not None
        result = context.settings_store.save(update.values)
        payload = result.as_dict()
        if result.retry_after > 0:
            return JSONResponse(
                payload,
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(result.retry_after)))},
            )
        if not result.ok:
            return JSONResponse(payload, status_code=422 if result.errors else 400)
        context.reload()
        return JSONResponse(payload)

    @router.post("/api/settings/check")
    def check_settings(request: Request) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        assert context.settings_store is not None
        result = context.settings_store.check()
        payload = result.as_dict()
        return JSONResponse(payload, status_code=200 if result.ok else 422)

    return router
