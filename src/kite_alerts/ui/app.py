"""The local FastAPI application.

API layout (C2 slots into this without moving anything):

============================  ======  ==========================================
Route                         Method  Purpose
============================  ======  ==========================================
``/api/status``               GET     bot health + today's buy/sell counters
``/api/alerts/recent``        GET     newest alerts for the dashboard feed
``/api/alerts/history``       GET     filtered, paginated read-only history
``/api/demo/timeline``        GET     the synthetic day (candles + alerts)
``/api/demo/stream``          GET     the same replay as NDJSON events
``/api/test-alert``           POST    one TEST message, rate limited
``/api/about``                GET     version, mode, alerts-only statement
============================  ======  ==========================================

C2 adds ``/api/settings`` (GET metadata, PUT values) and ``/api/kite/login``;
C3 adds ``/api/replay`` and ``/api/run`` (start/stop + log stream). See
``docs/ui-design.md``.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config.settings import Settings, load_settings
from ..core.errors import ConfigError
from .demo_player import DemoTimeline, warm_demo_timeline
from .history import AlertHistoryReader
from .security import SecurityMiddleware, require_same_origin
from .status import StatusService, http_probe
from .test_alert import NotifierFactory, TestAlertService

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
INDEX_FILE = STATIC_DIR / "index.html"
LICENSE_FILE = STATIC_DIR / "vendor" / "lightweight-charts" / "LICENSE"
NOTICE_FILE = STATIC_DIR / "vendor" / "lightweight-charts" / "NOTICE"

MODE = "Read-only dashboard with an offline demo player. " "The live feed is not started from here."

ALERTS_ONLY_STATEMENT = (
    "This tool only sends alerts. It never places, modifies or cancels orders, "
    "and it never touches your positions."
)

#: Exact attribution required by the vendored chart library (see its NOTICE).
ATTRIBUTION = {
    "name": "TradingView Lightweight Charts™",
    "version": "5.0.9",
    "license": "Apache-2.0",
    "notice": (
        "TradingView Lightweight Charts™\n"
        "Copyright (с) 2025 TradingView, Inc. https://www.tradingview.com/"
    ),
    "homepage": "https://www.tradingview.com/",
    "license_url": "/license",
    "notice_url": "/notice",
}

FIRST_DAY_CHECKLIST = (
    "Copy .env.example to .env and fill in KITE_API_KEY and KITE_API_SECRET.",
    "Add the instruments you watch, e.g. SYMBOLS=NSE:RELIANCE,NFO:NIFTY-FUT.",
    "Run `kite-alerts config-check` to confirm the settings are valid.",
    "Run `kite-alerts login` each morning — the token expires around 06:00 IST.",
    "Run `kite-alerts run` in its own window to start the alerts.",
    "Press the dashboard's Refresh status button; it should say “Bot running”.",
    "Try the demo player here first: it needs no account and no internet.",
)


@dataclass(frozen=True, slots=True)
class UiContext:
    """Everything the routes need, injectable for tests."""

    settings: Settings | None
    settings_error: str | None
    history: AlertHistoryReader
    status: StatusService
    test_alerts: TestAlertService
    demo: Callable[[], DemoTimeline] = warm_demo_timeline
    version: str = field(default=__version__)


def build_context(
    *,
    env_file: str | Path = ".env",
    db_path: str | Path | None = None,
    probe: Any | None = None,
    notifier_factory: NotifierFactory | None = None,
) -> UiContext:
    """Load settings once; a broken ``.env`` must not stop the demo from working."""
    settings: Settings | None = None
    settings_error: str | None = None
    try:
        settings = load_settings(env_file)
    except ConfigError as exc:
        settings_error = str(exc)
        log.warning("dashboard started without valid settings")

    path = (
        Path(db_path)
        if db_path is not None
        else (settings.db_path if settings is not None else Path("data/kite_alerts.db"))
    )
    history = AlertHistoryReader(path)
    status = StatusService(settings, probe=probe if probe is not None else http_probe)
    service_kwargs: dict[str, Any] = {"settings_error": settings_error}
    if notifier_factory is not None:
        service_kwargs["notifier_factory"] = notifier_factory
    test_alerts = TestAlertService(settings, **service_kwargs)
    return UiContext(
        settings=settings,
        settings_error=settings_error,
        history=history,
        status=status,
        test_alerts=test_alerts,
    )


def _ndjson(timeline: DemoTimeline) -> Iterator[str]:
    for event in timeline.events():
        yield json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n"


def create_app(context: UiContext) -> FastAPI:
    """Build the FastAPI app around an explicit context (no globals, easy to test)."""
    app = FastAPI(
        title="kite-alerts dashboard",
        version=context.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(SecurityMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(INDEX_FILE, media_type="text/html; charset=utf-8")

    @app.get("/license", include_in_schema=False)
    def license_text() -> FileResponse:
        return FileResponse(LICENSE_FILE, media_type="text/plain; charset=utf-8")

    @app.get("/notice", include_in_schema=False)
    def notice_text() -> FileResponse:
        return FileResponse(NOTICE_FILE, media_type="text/plain; charset=utf-8")

    @app.get("/api/about")
    def about() -> dict[str, Any]:
        return {
            "version": context.version,
            "mode": MODE,
            "alerts_only": True,
            "alerts_only_statement": ALERTS_ONLY_STATEMENT,
            "checklist": list(FIRST_DAY_CHECKLIST),
            "attribution": dict(ATTRIBUTION),
            "settings_error": context.settings_error,
            "demo": True,
        }

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "status": context.status.snapshot().as_dict(),
            "today": context.history.today_counts(),
            "version": context.version,
            "mode": MODE,
            "alerts_only": True,
            "settings_error": context.settings_error,
        }

    @app.get("/api/alerts/recent")
    def recent(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        return context.history.recent(limit=limit)

    @app.get("/api/alerts/history")
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

    @app.get("/api/demo/timeline")
    def demo_timeline() -> dict[str, Any]:
        return context.demo().payload()

    @app.get("/api/demo/stream")
    def demo_stream() -> StreamingResponse:
        timeline = context.demo()
        return StreamingResponse(
            _ndjson(timeline),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/test-alert")
    def test_alert(request: Request) -> JSONResponse:
        require_same_origin(request)
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

    return app
