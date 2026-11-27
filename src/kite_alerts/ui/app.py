"""The local FastAPI application.

API layout:

===========================  ======  ==========================================
Route                         Method  Purpose
===========================  ======  ==========================================
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
``/callback``
``/api/kite/manual-token``    POST    fallback: paste a request_token
``/api/kite/session``         GET     connected / expired / not connected
``/api/kite/disconnect``      POST    clear the saved token
``/api/data-sources``         GET     availability + reason per source
``/api/notifications/telegram/chat-id``  POST  find the user's chat id
``/api/practice/check``       POST    replay the demo with the saved settings
===========================  ======  ==========================================

The handlers live in :mod:`kite_alerts.ui.routes` (dashboard shell, overview,
alerts, settings, kite, chart, practice); this module owns the app object, the
lifespan, the security middleware and the static mount.

C3 adds ``/api/replay`` and ``/api/run`` (start/stop + log stream).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config.settings import Settings, load_settings
from ..core.errors import ConfigError
from .chart.service import ChartService
from .demo_player import DemoTimeline, warm_demo_timeline
from .history import AlertHistoryReader
from .kite_connect import KiteConnectService, default_kite_factory
from .notifications import TelegramSetupService
from .routes import (
    alerts_router,
    chart_router,
    dashboard_router,
    kite_router,
    notifications_router,
    overview_router,
    practice_router,
    settings_router,
)
from .routes.shared import (
    _INDEX_FALLBACK,
    ALERTS_ONLY_STATEMENT,
    ATTRIBUTION,
    FIRST_DAY_CHECKLIST,
    INDEX_FILE,
    LICENSE_FILE,
    MODE,
    NO_SETTINGS_MESSAGE,
    NOTICE_FILE,
    STATIC_DIR,
    SettingsUpdate,
    _error,
    _ndjson,
    _practice_payload,
    _resolved_paths,
    load_index_html,
)
from .security import SecurityMiddleware
from .settings_store import SettingsStore
from .status import StatusService, http_probe
from .test_alert import NotifierFactory, TestAlertService

log = logging.getLogger(__name__)

__all__ = [
    "ALERTS_ONLY_STATEMENT",
    "ATTRIBUTION",
    "FIRST_DAY_CHECKLIST",
    "INDEX_FILE",
    "LICENSE_FILE",
    "MODE",
    "NOTICE_FILE",
    "NO_SETTINGS_MESSAGE",
    "STATIC_DIR",
    "_INDEX_FALLBACK",
    "SettingsUpdate",
    "UiContext",
    "_error",
    "_ndjson",
    "_practice_payload",
    "_resolved_paths",
    "build_context",
    "create_app",
    "load_index_html",
]


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


def create_app(
    context: UiContext,
    *,
    allowed_hosts: frozenset[str] | set[str] | None = None,
) -> FastAPI:
    """Build the FastAPI app around an explicit context (no globals, easy to test).

    ``allowed_hosts`` pins the extra host names (besides loopback) that the
    security middleware accepts. ``None`` reads the environment, which is what
    the Vercel entrypoint relies on.
    """

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
    app.add_middleware(SecurityMiddleware, allowed_hosts=allowed_hosts)
    if STATIC_DIR.is_dir():
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

    app.include_router(dashboard_router())
    app.include_router(overview_router(context))
    app.include_router(alerts_router(context))
    app.include_router(settings_router(context))
    app.include_router(kite_router(context))
    app.include_router(notifications_router(context))
    app.include_router(chart_router(context))
    app.include_router(practice_router(context))

    return app
