"""Live runtime wiring.

``build_runtime`` is deliberately separate from ``run_live``: tests construct a
runtime with ``FakeKite``/``FakeTicker`` and drive ``worker.step()`` directly, so
the production wiring itself is what gets exercised offline.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..config.overrides import SymbolOverrides
from ..config.settings import Settings
from ..core.calendar import TradingCalendar, calendar_from_config
from ..core.clock import Clock, SystemClock, to_ist_ts
from ..core.errors import AuthError, ConfigError
from ..data.candle_builder import CandleBuilder
from ..data.feed import KiteFeed
from ..data.history import HistoryClient
from ..data.instruments import Instrument, InstrumentResolver
from ..data.watchdog import FeedWatchdog
from ..engine.engine import Engine
from ..engine.worker import MarketWorker
from ..notifiers.dlq import DeadLetterLog
from ..notifiers.factory import build_notifiers
from ..notifiers.hub import NotifierHub
from ..observability.health import HealthServer
from ..observability.logging import AuditLog
from ..observability.metrics import Metrics
from ..persistence.store import SqliteAlertStore

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_DATA = 4


@dataclass
class Runtime:
    settings: Settings
    calendar: TradingCalendar
    clock: Clock
    metrics: Metrics
    store: SqliteAlertStore
    audit: AuditLog
    hub: NotifierHub
    engine: Engine
    worker: MarketWorker
    feed: KiteFeed
    resolver: InstrumentResolver
    health: HealthServer | None = None
    instruments: tuple[Instrument, ...] = ()
    started: bool = field(default=False)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.started:
            return
        self.hub.start()
        if self.health is not None:
            self.health.start()
        self.feed.start()
        self.worker.start()
        self.started = True
        self.audit.record_event("started", symbols=list(self.worker.token_map))
        self.hub.send_text(
            f"✅ kite-alerts started — watching {', '.join(self.worker.token_map)} "
            "(ALERTS ONLY, no orders)",
            level="system",
            dedup_key="startup",
        )

    def shutdown(self, *, reason: str = "stopped", timeout: float = 20.0) -> None:
        log.info("shutting down (%s)", reason)
        try:
            self.hub.send_text(f"🛑 kite-alerts stopping ({reason})", level="system")
        except Exception:  # pragma: no cover - shutdown path
            log.debug("could not enqueue shutdown notice", exc_info=True)
        self.worker.stop(timeout=min(timeout, 10.0))
        self.feed.stop()
        self.hub.close(timeout=timeout)
        if self.health is not None:
            self.health.stop()
        try:
            self.store.close()
        finally:
            self.audit.record_event("stopped", reason=reason)
            self.started = False

    # ------------------------------------------------------------------ health
    def health_status(self) -> dict[str, Any]:
        worker = self.worker.health()
        in_session = bool(worker.get("in_session"))
        age = worker.get("last_tick_age_seconds")
        if in_session and age is None:
            # No tick yet today: age the feed from the session open, so /health
            # reports unhealthy when a session starts silent.
            age = round(self.calendar.session_minutes_elapsed(self.clock.now()) * 60.0, 1)
        stale = bool(
            in_session
            and isinstance(age, (int, float))
            and age > self.settings.resilience.feed_stale_seconds
        )
        healthy = not worker.get("auth_failed") and not stale
        if not in_session:
            healthy = not worker.get("auth_failed")
        status = "ok" if healthy else ("auth_failed" if worker.get("auth_failed") else "stale_feed")
        return {
            "healthy": healthy,
            "status": status,
            "service": "kite-alerts",
            "session": worker.get("session"),
            "in_session": in_session,
            "last_tick_age_seconds": age,
            "queue_depth": worker.get("queue_depth"),
            "reconnects": worker.get("reconnects"),
            "symbols": list(self.worker.token_map),
            "notifications": self.hub.health(),
            "engine": worker.get("engine"),
        }


def _kite_client(settings: Settings) -> Any:
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=settings.kite.api_key)
    kite.set_access_token(settings.kite.token)
    return kite


def _verify_session(kite: Any, settings: Settings) -> None:
    """Fail fast with an actionable message instead of a silent dead feed."""
    profile = getattr(kite, "profile", None)
    if not callable(profile):
        return
    try:
        profile()
    except Exception as exc:
        from ..core.errors import classify_exception

        if classify_exception(exc) == "auth":
            raise AuthError(
                "Kite rejected the access token at startup. Tokens expire daily (~06:00 IST): "
                "run `kite-alerts login` and restart."
            ) from exc
        log.warning("could not verify the session at startup: %s", exc)


def build_runtime(
    settings: Settings,
    *,
    kite: Any | None = None,
    ticker_factory: Callable[..., Any] | None = None,
    clock: Clock | None = None,
    notifiers: Iterable[Any] | None = None,
    start_health: bool = True,
    verify_session: bool = True,
) -> Runtime:
    clock = clock or SystemClock()
    settings.kite.require()

    # Verify the session before creating any durable state: an expired token must
    # fail with one clear message, not leave a database handle behind.
    active_kite = kite if kite is not None else _kite_client(settings)
    if verify_session:
        _verify_session(active_kite, settings)

    calendar = calendar_from_config(
        open_value=settings.session.open_time,
        close_value=settings.session.close_time,
        holiday_file=settings.session.holiday_file,
        special_sessions_file=settings.session.special_sessions_file,
        pre_open_value=settings.session.pre_open_time,
        closing_value=settings.session.closing_end,
    )
    metrics = Metrics()
    log_dir = settings.observability.log_dir
    audit = AuditLog(log_dir / "alerts-audit.jsonl")
    store = SqliteAlertStore(settings.db_path)

    notifier_list = list(notifiers) if notifiers is not None else build_notifiers(settings.notify)
    if not notifier_list:
        store.close()
        raise ConfigError("no notification channels configured")
    hub = NotifierHub(
        notifier_list,
        settings=settings.notify,
        metrics=metrics,
        dlq=DeadLetterLog(log_dir / "dead-letter.jsonl"),
    )

    resolver = InstrumentResolver(active_kite, settings.cache_dir, clock=clock, metrics=metrics)
    resolved, errors = resolver.resolve_many(settings.symbols)
    if not resolved:
        detail = "; ".join(f"{spec}: {err}" for spec, err in errors.items()) or "no symbols"
        raise ConfigError(f"none of the configured symbols could be resolved ({detail})")
    for spec, error in errors.items():
        log.error("skipping %s: %s", spec, error)

    token_map = {symbol: instrument.token for symbol, instrument in resolved.items()}
    futures = frozenset(symbol for symbol, inst in resolved.items() if inst.is_future)

    overrides = SymbolOverrides.load(settings.overrides_file, settings.params)
    aliases = resolver.aliases

    def params_for(symbol: str) -> Any:
        return overrides.for_symbol(symbol, aliases)

    history = HistoryClient(active_kite, settings=settings.resilience, metrics=metrics, clock=clock)
    engine = Engine(
        params_for=params_for,
        store=store,
        sink=hub,
        calendar=calendar,
        metrics=metrics,
    )

    builder = CandleBuilder(metrics=metrics)
    watchdog = FeedWatchdog(
        calendar=calendar,
        symbols=tuple(token_map),
        feed_stale_seconds=settings.resilience.feed_stale_seconds,
        symbol_stale_seconds=settings.resilience.symbol_stale_seconds,
        clock=clock,
        metrics=metrics,
    )
    worker = MarketWorker(
        builder=builder,
        engine=engine,
        calendar=calendar,
        settings=settings,
        hub=hub,
        metrics=metrics,
        clock=clock,
        history=history,
        token_map=token_map,
        futures=futures,
        warmup_days=settings.warmup_days,
        watchdog=watchdog,
    )

    def on_connect(first: bool) -> None:
        worker.request_backfill(first_connect=first)

    def on_auth_error(message: str) -> None:
        worker._fail_auth(message)

    feed = KiteFeed(
        api_key=settings.kite.api_key,
        access_token=settings.kite.token,
        token_map=token_map,
        sink=worker.enqueue_tick,
        settings=settings.resilience,
        metrics=metrics,
        clock=clock,
        ticker_factory=ticker_factory,
        on_connect_hook=on_connect,
        on_auth_error=on_auth_error,
    )
    worker.attach_feed(feed)

    runtime = Runtime(
        settings=settings,
        calendar=calendar,
        clock=clock,
        metrics=metrics,
        store=store,
        audit=audit,
        hub=hub,
        engine=engine,
        worker=worker,
        feed=feed,
        resolver=resolver,
        instruments=tuple(resolved.values()),
    )

    if start_health and settings.observability.http_enabled:
        runtime.health = HealthServer(
            host=settings.observability.http_host,
            port=settings.observability.http_port,
            status_provider=runtime.health_status,
            metrics_provider=metrics.render_prometheus,
        )

    try:
        _warm_up(runtime, history, resolved, settings, calendar, clock, futures)
    except Exception:
        store.close()
        raise
    _log_expiry_warnings(resolver, resolved, hub)
    return runtime


def _warm_up(
    runtime: Runtime,
    history: HistoryClient,
    resolved: Mapping[str, Instrument],
    settings: Settings,
    calendar: TradingCalendar,
    clock: Clock,
    futures: frozenset[str],
) -> None:
    now = to_ist_ts(clock.now())
    empty: list[str] = []
    for symbol, instrument in resolved.items():
        try:
            frame = history.fetch(
                instrument.token,
                days=settings.warmup_days,
                continuous=symbol in futures,
            )
        except Exception as exc:
            from ..core.errors import classify_exception

            if classify_exception(exc) == "auth":
                raise AuthError(
                    "Kite rejected the access token while seeding history. "
                    "Run `kite-alerts login` and restart."
                ) from exc
            log.error("warm-up failed for %s: %s", symbol, exc)
            empty.append(symbol)
            continue
        if frame.empty:
            empty.append(symbol)
            log.warning("warm-up for %s returned no candles", symbol)
            continue
        bars = runtime.engine.seed(symbol, frame)
        log.info("seeded %s with %d candles (buffer=%d)", symbol, len(frame), bars)
        runtime.metrics.observe("warmup_bars", float(len(frame)), symbol=symbol)

    if empty:
        reason = calendar.describe(now)
        log.warning(
            "no history for %s (session state: %s). If the market is closed this is expected; "
            "otherwise check the symbol, the token and historical-data entitlement.",
            ", ".join(empty),
            reason,
        )


def _log_expiry_warnings(
    resolver: InstrumentResolver, resolved: Mapping[str, Instrument], hub: NotifierHub
) -> None:
    warnings = resolver.expiry_warnings(resolved.values())
    for warning in warnings:
        log.warning(warning)
        hub.send_text(f"⏳ {warning}", level="system")


def _install_signal_handlers(stop: threading.Event) -> dict[int, Any]:
    previous: dict[int, Any] = {}
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue

        def handler(signum: int, _frame: Any, _sig: Any = sig) -> None:
            log.info("received %s; shutting down gracefully", signal.Signals(signum).name)
            stop.set()

        try:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, handler)
        except (ValueError, OSError):  # not in the main thread (e.g. tests)
            continue
    return previous


def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover
            continue


def run_live(
    settings: Settings,
    *,
    runtime: Runtime | None = None,
    duration_seconds: float | None = None,
    install_signal_handlers: bool = True,
) -> int:
    """Run until stopped. Returns a process exit code."""
    runtime = runtime or build_runtime(settings)
    stop = threading.Event()
    previous = _install_signal_handlers(stop) if install_signal_handlers else {}

    exit_code = EXIT_OK
    try:
        runtime.start()
        deadline = time.monotonic() + duration_seconds if duration_seconds is not None else None
        while not stop.is_set() and not runtime.worker.stop_event.is_set():
            runtime.worker.step()
            if deadline is not None and time.monotonic() >= deadline:
                log.info("configured duration elapsed")
                break
    except AuthError as exc:
        log.error("%s", exc)
        runtime.hub.send_text(f"🔐 {exc}", level="error")
        exit_code = EXIT_AUTH
    except KeyboardInterrupt:  # pragma: no cover - interactive
        log.info("interrupted")
    except Exception:
        log.exception("fatal error in the live loop")
        exit_code = EXIT_ERROR
    finally:
        if runtime.worker.auth_failed:
            exit_code = EXIT_AUTH
        runtime.shutdown(reason="shutdown")
        _restore_signal_handlers(previous)
    return exit_code
