"""Chart workstation routes: candles, live ticks, clock and watchlist."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..chart.types import ChartError
from ..live_feed import SSEClient
from ..security import require_csrf_token, require_same_origin
from .shared import WatchlistSymbol, _error

if TYPE_CHECKING:
    from ..app import UiContext


def chart_router(context: UiContext) -> APIRouter:
    """Build the router for the chart workstation."""
    router = APIRouter()

    def chart_guard() -> None:
        assert context.chart is not None
        if not context.chart.allow_request():
            raise HTTPException(
                status_code=429,
                detail="The chart is refreshing too often. Wait a few seconds and try again.",
            )

    def chart_failure(exc: ChartError) -> JSONResponse:
        return _error(exc.status, exc.message, exc.code)

    def _ensure_feed_for(source: str, symbol: str) -> None:
        """Selecting a live symbol is what starts/extends the ticker (F1)."""
        if source != "kite_live" or context.feed_controller is None:
            return
        spec = symbol.strip()
        context.feed_controller.ensure_started([spec] if spec else None)

    @router.get("/api/chart/symbols")
    def chart_symbols(
        source: Annotated[str, Query(max_length=32)] = "nse_delayed",
        refresh: bool = False,
    ) -> JSONResponse:
        chart_guard()
        assert context.chart is not None
        if refresh:
            context.chart.clear_caches()
        try:
            return JSONResponse(context.chart.symbols_payload(source))
        except ChartError as exc:
            return chart_failure(exc)

    @router.get("/api/chart/candles")
    def chart_candles(
        source: Annotated[str, Query(max_length=32)] = "nse_delayed",
        symbol: Annotated[str, Query(max_length=80)] = "NSE:RELIANCE",
        interval: Annotated[int, Query(ge=1, le=15)] = 5,
        limit: Annotated[int, Query(ge=10, le=600)] = 300,
    ) -> JSONResponse:
        chart_guard()
        assert context.chart is not None
        _ensure_feed_for(source, symbol)
        try:
            return JSONResponse(
                context.chart.candles(
                    source,
                    symbol,
                    interval=interval,
                    limit=limit,
                )
            )
        except ChartError as exc:
            return chart_failure(exc)

    @router.get("/api/chart/live")
    def chart_live(
        source: Annotated[str, Query(max_length=32)] = "nse_delayed",
        symbol: Annotated[str, Query(max_length=80)] = "NSE:RELIANCE",
        interval: Annotated[int, Query(ge=1, le=15)] = 1,
    ) -> JSONResponse:
        chart_guard()
        assert context.chart is not None
        _ensure_feed_for(source, symbol)
        try:
            return JSONResponse(context.chart.live(source, symbol, interval=interval))
        except ChartError as exc:
            return chart_failure(exc)

    @router.get("/api/chart/clock")
    def chart_clock() -> JSONResponse:
        chart_guard()
        assert context.chart is not None
        return JSONResponse(context.chart.clock_payload())

    @router.get("/api/chart/watchlist")
    def chart_watchlist(
        source: Annotated[str, Query(max_length=32)] = "nse_delayed",
    ) -> JSONResponse:
        chart_guard()
        assert context.chart is not None
        try:
            return JSONResponse(context.chart.watchlist(source))
        except ChartError as exc:
            return chart_failure(exc)

    @router.post("/api/chart/watchlist")
    def chart_watchlist_add(request: Request, payload: WatchlistSymbol) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        chart_guard()
        assert context.chart is not None
        try:
            return JSONResponse(context.chart.add_watchlist(payload.source, payload.symbol))
        except ChartError as exc:
            return chart_failure(exc)

    @router.delete("/api/chart/watchlist")
    def chart_watchlist_remove(request: Request, payload: WatchlistSymbol) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        chart_guard()
        assert context.chart is not None
        try:
            return JSONResponse(context.chart.remove_watchlist(payload.source, payload.symbol))
        except ChartError as exc:
            return chart_failure(exc)

    @router.get("/api/chart/stream")
    async def chart_stream(
        request: Request,
        source: Annotated[str, Query(max_length=32)] = "kite_live",
        symbol: Annotated[str, Query(max_length=80)] = "",
        interval: Annotated[int, Query(ge=1, le=15)] = 1,
    ) -> StreamingResponse:
        chart_guard()
        if source != "kite_live":
            raise HTTPException(
                status_code=422,
                detail="Streaming is only supported for the live Kite feed.",
            )
        if context.feed is None:
            raise HTTPException(status_code=503, detail="Live feed service is not available.")
        _ensure_feed_for(source, symbol)

        client = SSEClient(symbol=symbol, interval=interval)
        # Cap check and registration happen under one lock: two tabs opening
        # together can no longer both pass a stale len() check.
        if not context.feed.admit_sse_client(client):
            raise HTTPException(
                status_code=429,
                detail="Too many concurrent stream connections. Try again later.",
            )

        async def event_generator() -> AsyncIterator[str]:
            try:
                st = context.feed.status().as_dict()
                yield f"event: status\ndata: {json.dumps(st)}\n\n"

                last_heartbeat = time.monotonic()
                while not await request.is_disconnected():
                    # client.get() blocks; run it in a worker thread so the
                    # event loop keeps serving every other request (F17).
                    event = await asyncio.to_thread(client.get, 0.5)
                    while event is not None:
                        yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
                        event = client.get(timeout=0)
                    now = time.monotonic()
                    if now - last_heartbeat >= 15.0:
                        last_heartbeat = now
                        yield ": heartbeat\n\n"
                    await asyncio.sleep(0)
            finally:
                context.feed.unregister_sse_client(client)
                client.close()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/api/chart/feed-status")
    def chart_feed_status() -> JSONResponse:
        chart_guard()
        if context.feed is None:
            return JSONResponse({"ok": False, "status": None, "is_simulated": False})
        return JSONResponse(
            {
                "ok": True,
                "status": context.feed.status().as_dict(),
                "is_simulated": bool(
                    context.simulate_kite or getattr(context.feed, "is_simulated", False)
                ),
            }
        )

    return router
