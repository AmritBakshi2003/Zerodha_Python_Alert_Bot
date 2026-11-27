"""Alert feed, demo timeline/stream and one-shot test-alert routes."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..security import require_csrf_token, require_same_origin
from .shared import _ndjson

if TYPE_CHECKING:
    from ..app import UiContext


def alerts_router(context: UiContext) -> APIRouter:
    """Build the router for alert reads, the demo replay and the test alert."""
    router = APIRouter()

    @router.get("/api/alerts/recent")
    def recent(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        return context.history.recent(limit=limit)

    @router.get("/api/alerts/history")
    def history(
        symbol: Annotated[str | None, Query(max_length=40)] = None,
        side: Annotated[str | None, Query(max_length=8)] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        try:
            page_result = context.history.read_page(
                symbol=symbol, side=side, page=page, page_size=page_size
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return page_result.as_dict()

    @router.get("/api/demo/timeline")
    def demo_timeline() -> dict[str, Any]:
        return context.demo().payload()

    @router.get("/api/demo/stream")
    def demo_stream() -> StreamingResponse:
        timeline = context.demo()
        return StreamingResponse(
            _ndjson(timeline),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/test-alert")
    def test_alert(request: Request) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        outcome = context.test_alerts.send()
        payload = outcome.as_dict()
        if outcome.retry_after > 0:
            return JSONResponse(
                payload,
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(outcome.retry_after)))},
            )
        if context.settings is None:
            return JSONResponse(payload, status_code=503)
        return JSONResponse(payload)

    return router
