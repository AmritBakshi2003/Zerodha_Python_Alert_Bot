"""The local FastAPI application.

API layout:

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
``/api/settings``             GET     field metadata, never a secret value
``/api/settings``             PUT     validate + atomic write to .env
``/api/settings/check``       POST    the same validation as config-check
``/api/kite/login``           POST    start Zerodha's own login redirect
``/callback``                 GET     loopback redirect: store the daily token
``/api/kite/manual-token``    POST    fallback: paste a request_token
``/api/kite/session``         GET     connected / expired / not connected
``/api/kite/disconnect``      POST    clear the saved token
``/api/data-sources``         GET     availability + reason per source
``/api/notifications/telegram/chat-id``  POST  find the user's chat id
``/api/practice/check``       POST    replay the demo with the saved settings
============================  ======  ==========================================

C3 adds ``/api/replay`` and ``/api/run`` (start/stop + log stream).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..config.settings import Settings, load_settings
from ..core.errors import ConfigError
from .chart.service import ChartService
from .chart.types import ChartError
from .demo_player import (
    DemoTimeline,
    build_demo_timeline_for,
    warm_demo_timeline,
)
from .history import AlertHistoryReader
from .kite_connect import (
    STATE_COOKIE,
    ConnectError,
    KiteConnectService,
    default_kite_factory,
)
from .notifications import TelegramSetupService
from .security import SecurityMiddleware, require_same_origin
from .settings_schema import connect_payload, settings_payload
from .settings_store import SettingsStore
from .status import StatusService, http_probe
from .test_alert import NotifierFactory, TestAlertService

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
INDEX_FILE = STATIC_DIR / "index.html"
LICENSE_FILE = STATIC_DIR / "vendor" / "lightweight-charts" / "LICENSE"
NOTICE_FILE = STATIC_DIR / "vendor" / "lightweight-charts" / "NOTICE"

MODE = (
    "Read-only dashboard with a practice replay, a guided Kite connection and settings. "
    "The live alert feed is started with `kite-alerts run`, not from here."
)

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
    "Open Connect and create your own Kite Connect app at developers.kite.trade.",
    "Copy this dashboard's redirect URL into that app's settings.",
    "Paste your own API key and secret on Connect, then log in on Zerodha's page.",
    "On Settings, choose the instruments you watch and enable Telegram or Slack.",
    "Run `kite-alerts config-check` (or press Check settings) to confirm the file.",
    "Run `kite-alerts run` in its own window; the dashboard only watches it.",
    "Try the Practice page first: it needs no account and no internet.",
)

NO_SETTINGS_MESSAGE = (
    "Your settings could not be read. The practice replay still works, but connecting, "
    "saving and status need a valid .env."
)


class SettingsUpdate(BaseModel):
    """A partial settings update, keyed by environment variable name."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(default_factory=dict)


class ManualToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_token: str = ""
    state: str = ""


class WatchlistSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "nse_delayed"
    symbol: str = ""


@dataclass
class UiContext:
    """Everything the routes need, injectable for tests."""

    settings: Settings | None
    settings_error: str | None
    history: AlertHistoryReader
    status: StatusService
    test_alerts: TestAlertService
    demo: Callable[[], DemoTimeline] = warm_demo_timeline
    version: str = field(default=__version__)
    env_file: Path = Path(".env")
    port: int = 8765
    settings_store: SettingsStore | None = None
    kite: KiteConnectService | None = None
    telegram: TelegramSetupService | None = None
    kite_factory: Callable[[str, str], Any] = default_kite_factory
    http_get: Any = None
    chart: ChartService | None = None
    feed: Any = None
    simulator: Any = None
    simulate_kite: bool = False
    ticker_factory: Any = None
    resolver_factory: Any = None
    feed_controller: Any = None

    def __post_init__(self) -> None:
        if self.settings_store is None:
            self.settings_store = SettingsStore(self.env_file)
        if self.kite is None:
            self.kite = KiteConnectService(
                self.settings_store,
                lambda: self.settings,
                port=self.port,
                on_change=self.reload,
                kite_factory=self.kite_factory,
                feed_provider=lambda: self.feed,
            )
        else:
            self.kite.feed_provider = lambda: self.feed
        if self.telegram is None:
            kwargs: dict[str, Any] = {}
            if self.http_get is not None:
                kwargs["http_get"] = self.http_get
            self.telegram = TelegramSetupService(lambda: self.settings, **kwargs)
        if self.chart is None:
            self.chart = ChartService(
                settings_provider=lambda: self.settings,
                watchlist_path=self.env_file.parent / "data" / "chart_watchlist.json",
            )
        if self.feed is None:
            from .live_feed import LiveFeedService

            def _make_ticker(api_key: str, access_token: str) -> Any:
                if self.ticker_factory is not None:
                    return self.ticker_factory(api_key, access_token)
                from kiteconnect import KiteTicker

                return KiteTicker(api_key, access_token)

            def _make_kite_for_feed() -> Any:
                if self.settings and self.settings.kite.api_key:
                    return self.kite_factory(self.settings.kite.api_key, self.settings.kite.token)
                return None

            calendar = None
            if self.chart is not None:
                calendar = self.chart._calendar(self.settings)

            self.feed = LiveFeedService(
                ticker_factory=_make_ticker,
                kite_factory=_make_kite_for_feed,
                calendar=calendar,
                is_simulated=self.simulate_kite,
            )

        if self.chart is not None and "kite_live" in self.chart.sources:
            live_src = self.chart.sources["kite_live"]
            if hasattr(live_src, "feed_provider"):
                live_src.feed_provider = lambda: self.feed

        if self.simulate_kite and self.simulator is None and self.feed is not None:
            from .simulator import KiteSimulator

            self.simulator = KiteSimulator(self.feed)
            self.simulator.start()

        if self.feed_controller is None and self.feed is not None:
            from .feed_controller import FeedController, default_resolver_factory

            self.feed_controller = FeedController(
                self.feed,
                lambda: self.settings,
                resolver_factory=self.resolver_factory or default_resolver_factory,
            )

    # ------------------------------------------------------------------ lifecycle
    def reload(self) -> None:
        """Re-read .env after a save or a successful login."""
        settings: Settings | None
        error: str | None
        try:
            settings = load_settings(self.env_file)
            error = None
        except ConfigError as exc:
            settings = None
            error = str(exc)
        self.settings = settings
        self.settings_error = error
        path = settings.db_path if settings is not None else self.history.path
        self.history = AlertHistoryReader(path, clock=self.history.clock)
        self.status.update_settings(settings)
        self.test_alerts.settings = settings
        self.test_alerts.settings_error = error
        # A saved key or a fresh token means the feed must (re)connect.
        if self.feed_controller is not None:
            self.feed_controller.reset()
            self.feed_controller.ensure_started()

    def session_status(self) -> dict[str, Any]:
        assert self.kite is not None
        return self.kite.session().as_dict()


def build_context(
    *,
    env_file: str | Path = ".env",
    db_path: str | Path | None = None,
    probe: Any | None = None,
    notifier_factory: NotifierFactory | None = None,
    port: int = 8765,
    kite_factory: Callable[[str, str], Any] = default_kite_factory,
    http_get: Any = None,
    chart: ChartService | None = None,
    feed: Any = None,
    ticker_factory: Any = None,
    simulate_kite: bool = False,
) -> UiContext:
    """Load settings once; a broken ``.env`` must not stop the practice replay."""
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
        env_file=Path(env_file),
        port=port,
        kite_factory=kite_factory,
        http_get=http_get,
        chart=chart,
        feed=feed,
        ticker_factory=ticker_factory,
        simulate_kite=simulate_kite,
    )


def _ndjson(timeline: DemoTimeline) -> Iterator[str]:
    for event in timeline.events():
        yield json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n"


def _error(status: int, message: str, code: str = "error") -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message}, status_code=status)


def _practice_payload(timeline: DemoTimeline, version: str) -> dict[str, Any]:
    alerts = []
    for alert in timeline.alerts:
        alerts.append(
            {
                "side": alert.side,
                "ts": alert.ts,
                "time": alert.chart_time,
                "price": alert.price,
                "reasons": list(alert.reasons),
                "rules": [dict(rule) for rule in alert.rules],
                "rules_agreed": [
                    str(rule["label"]) for rule in alert.rules if rule["status"] == "agree"
                ],
            }
        )
    total = len(timeline.alerts)
    message = (
        f"With your saved settings, this practice day produces {total} alert(s): "
        f"{timeline.sells} SELL and {timeline.buys} BUY. Nothing is saved."
    )
    return {
        "ok": True,
        "demo": True,
        "label": "DEMO",
        "version": version,
        "symbol": timeline.symbol,
        "counts": {
            "buys": timeline.buys,
            "sells": timeline.sells,
            "total": total,
        },
        "alerts": alerts,
        "message": message,
    }


def create_app(context: UiContext) -> FastAPI:
    """Build the FastAPI app around an explicit context (no globals, easy to test)."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # F1: nothing used to start the feed, so ``live`` was advertised but dead.
        if context.feed_controller is not None:
            context.feed_controller.ensure_started()
        yield
        if context.simulator is not None:
            context.simulator.stop()
        if context.feed is not None:
            context.feed.stop()

    app = FastAPI(
        title="kite-alerts dashboard",
        version=context.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(SecurityMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = []
        for err in exc.errors():
            item = dict(err)
            item.pop("url", None)
            errors.append(item)
        return JSONResponse({"detail": errors}, status_code=422)

    def require_settings() -> Settings:
        if context.settings is None:
            raise HTTPException(status_code=503, detail=NO_SETTINGS_MESSAGE)
        return context.settings

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
        settings = context.settings
        return {
            "version": context.version,
            "mode": MODE,
            "alerts_only": True,
            "alerts_only_statement": ALERTS_ONLY_STATEMENT,
            "checklist": list(FIRST_DAY_CHECKLIST),
            "attribution": dict(ATTRIBUTION),
            "settings_error": context.settings_error,
            "kite_configured": bool(settings and settings.kite.api_key),
            "token_present": bool(settings and settings.kite.token),
            "simulate_kite": context.simulate_kite,
            "demo": True,
            "redirect_url": context.kite.redirect_url if context.kite else "",
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
            "warnings": context.settings.warnings_list() if context.settings else [],
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

    # ------------------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        assert context.settings_store is not None
        return settings_payload(
            context.settings,
            settings_error=context.settings_error,
            env_file=str(context.settings_store.path),
            connect=connect_payload(context.settings, settings_error=context.settings_error),
        )

    @app.put("/api/settings")
    def put_settings(request: Request, update: SettingsUpdate) -> JSONResponse:
        require_same_origin(request)
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

    @app.post("/api/settings/check")
    def check_settings(request: Request) -> JSONResponse:
        require_same_origin(request)
        assert context.settings_store is not None
        result = context.settings_store.check()
        payload = result.as_dict()
        return JSONResponse(payload, status_code=200 if result.ok else 422)

    # ----------------------------------------------------------------------- kite
    @app.post("/api/kite/login")
    def kite_login(request: Request) -> JSONResponse:
        require_same_origin(request)
        assert context.kite is not None
        try:
            start = context.kite.start()
        except ConnectError as exc:
            return _error(400, exc.message, exc.code)
        response = JSONResponse(
            {
                "ok": True,
                "start_url": start.start_url,
                "redirect_url": start.redirect_url,
                "expires_in": start.expires_in,
                "message": (
                    "Opening Zerodha's login page. Log in there — this app never sees your "
                    "password or one-time code."
                ),
            }
        )
        response.set_cookie(
            STATE_COOKIE,
            start.state,
            max_age=start.expires_in,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/kite/start", include_in_schema=False)
    def kite_start() -> RedirectResponse:
        assert context.kite is not None
        target = context.kite.pending_login_url()
        if target is None:
            return RedirectResponse("/?connect=no_attempt#/connect", status_code=303)
        return RedirectResponse(target, status_code=302)

    def _failed_callback(code: str) -> RedirectResponse:
        return RedirectResponse(f"/?connect={code}#/connect", status_code=303)

    @app.get("/callback", include_in_schema=False)
    def callback(
        request: Request,
        request_token: str = "",
        state: str = "",
        status: str = "",
    ) -> RedirectResponse:
        assert context.kite is not None
        if status and status != "success":
            context.kite.cancel_pending()
            return _failed_callback("cancelled")
        effective_state = state or request.cookies.get(STATE_COOKIE, "")
        try:
            context.kite.finish(request_token, effective_state)
        except ConnectError as exc:
            return _failed_callback(exc.code)
        response = RedirectResponse("/?connect=connected#/connect", status_code=303)
        response.delete_cookie(STATE_COOKIE, path="/")
        return response

    @app.post("/api/kite/manual-token")
    def manual_token(request: Request, payload: ManualToken) -> JSONResponse:
        require_same_origin(request)
        assert context.kite is not None
        effective_state = payload.state or request.cookies.get(STATE_COOKIE, "")
        try:
            result = context.kite.finish(payload.request_token, effective_state)
        except ConnectError as exc:
            return _error(400, exc.message, exc.code)
        return JSONResponse(result.as_dict())

    @app.get("/api/kite/session")
    def kite_session() -> dict[str, Any]:
        assert context.kite is not None
        session = context.kite.session()
        payload = session.as_dict()
        payload["redirect_url"] = context.kite.redirect_url
        payload["token_present"] = bool(context.settings and context.settings.kite.token)
        payload["credentials"] = connect_payload(
            context.settings, settings_error=context.settings_error
        )
        return payload

    @app.post("/api/kite/disconnect")
    def kite_disconnect(request: Request) -> JSONResponse:
        require_same_origin(request)
        assert context.kite is not None
        return JSONResponse(context.kite.disconnect().as_dict())

    @app.get("/api/data-sources")
    def data_sources(refresh: bool = False) -> dict[str, Any]:
        assert context.kite is not None
        return context.kite.data_sources(force=refresh)

    # ---------------------------------------------------------------------- chart
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

    @app.get("/api/chart/symbols")
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

    @app.get("/api/chart/candles")
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

    @app.get("/api/chart/live")
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

    @app.get("/api/chart/clock")
    def chart_clock() -> JSONResponse:
        chart_guard()
        assert context.chart is not None
        return JSONResponse(context.chart.clock_payload())

    @app.get("/api/chart/watchlist")
    def chart_watchlist(
        source: Annotated[str, Query(max_length=32)] = "nse_delayed",
    ) -> JSONResponse:
        chart_guard()
        assert context.chart is not None
        try:
            return JSONResponse(context.chart.watchlist(source))
        except ChartError as exc:
            return chart_failure(exc)

    @app.post("/api/chart/watchlist")
    def chart_watchlist_add(request: Request, payload: WatchlistSymbol) -> JSONResponse:
        require_same_origin(request)
        chart_guard()
        assert context.chart is not None
        try:
            return JSONResponse(context.chart.add_watchlist(payload.source, payload.symbol))
        except ChartError as exc:
            return chart_failure(exc)

    @app.delete("/api/chart/watchlist")
    def chart_watchlist_remove(request: Request, payload: WatchlistSymbol) -> JSONResponse:
        require_same_origin(request)
        chart_guard()
        assert context.chart is not None
        try:
            return JSONResponse(context.chart.remove_watchlist(payload.source, payload.symbol))
        except ChartError as exc:
            return chart_failure(exc)

    @app.get("/api/chart/stream")
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

        from .live_feed import SSEClient

        client = SSEClient(symbol=symbol, interval=interval)
        # Cap check and registration happen under one lock: two tabs opening
        # together can no longer both pass a stale len() check.
        if not context.feed.admit_sse_client(client):
            raise HTTPException(
                status_code=429,
                detail="Too many concurrent stream connections. Try again later.",
            )

        async def event_generator() -> AsyncIterator[str]:
            import asyncio
            import time

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

    @app.get("/api/chart/feed-status")
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

    @app.post("/api/kite/simulate/disconnect")
    def sim_disconnect(request: Request) -> JSONResponse:
        require_same_origin(request)
        if not context.simulate_kite or context.simulator is None:
            raise HTTPException(status_code=400, detail="Simulator mode is not active.")
        context.simulator.trigger_disconnect()
        return JSONResponse({"ok": True, "action": "disconnect"})

    @app.post("/api/kite/simulate/token-expiry")
    def sim_token_expiry(request: Request) -> JSONResponse:
        require_same_origin(request)
        if not context.simulate_kite or context.simulator is None:
            raise HTTPException(status_code=400, detail="Simulator mode is not active.")
        context.simulator.trigger_token_expiry()
        return JSONResponse({"ok": True, "action": "token_expiry"})

    @app.post("/api/kite/simulate/reconnect")
    def sim_reconnect(request: Request) -> JSONResponse:
        require_same_origin(request)
        if not context.simulate_kite or context.simulator is None:
            raise HTTPException(status_code=400, detail="Simulator mode is not active.")
        context.simulator.trigger_reconnect()
        return JSONResponse({"ok": True, "action": "reconnect"})

    # -------------------------------------------------------------- notifications
    @app.post("/api/notifications/telegram/chat-id")
    def telegram_chat_id(request: Request) -> JSONResponse:
        require_same_origin(request)
        assert context.telegram is not None
        return JSONResponse(context.telegram.find_chat_id().as_dict())

    # ------------------------------------------------------------------- practice
    @app.post("/api/practice/check")
    def practice_check(request: Request) -> JSONResponse:
        require_same_origin(request)
        if context.settings is None:
            return _error(503, NO_SETTINGS_MESSAGE, "no_settings")
        timeline = build_demo_timeline_for(context.settings.params)
        return JSONResponse(_practice_payload(timeline, context.version))

    return app
