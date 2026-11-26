"""F11 — startup, shutdown and health fault paths for the live runtime.

These tests exercise ``build_runtime`` / ``run_live`` exactly as ``kite-alerts
run`` does, with the fake Kite objects, so the paths that fail at 09:15 have a
regression test: missing/expired token, no notifier, unknown symbol, a taken
health port, a raising run loop and the exit code for each.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pandas as pd
import pytest
from pydantic import SecretStr

from e2e.support import build_harness, live_settings
from fakes.fake_kite import FakeInstrument, FakeKite, RecordingNotifier, TokenException
from kite_alerts.cli.run import (
    EXIT_AUTH,
    EXIT_ERROR,
    EXIT_OK,
    _install_signal_handlers,
    _restore_signal_handlers,
    build_runtime,
    run_live,
)
from kite_alerts.config.settings import KiteCredentials
from kite_alerts.core.errors import AuthError, ConfigError
from kite_alerts.demo import synthetic_day
from kite_alerts.observability.health import HealthServer
from kite_alerts.persistence.store import SqliteAlertStore

TOKEN = 111


@pytest.fixture
def warmup() -> pd.DataFrame:
    return synthetic_day().iloc[:50]


def _kite(warmup: pd.DataFrame, **kwargs: object) -> FakeKite:
    return FakeKite(
        instruments=[FakeInstrument(TOKEN, "NSE", "TEST", name="TEST")],
        history={TOKEN: warmup},
        **kwargs,  # type: ignore[arg-type]
    )


class TestRuntimeLifecycle:
    def test_start_is_idempotent_and_shutdown_is_terminal(self, tmp_path: Path) -> None:
        harness = build_harness(tmp_path)
        runtime = harness.runtime
        runtime.start()
        runtime.start()  # no-op, not a second feed/worker

        assert runtime.started is True
        assert harness.ticker.connected is True
        audit_path = runtime.settings.observability.log_dir / "alerts-audit.jsonl"
        assert '"started"' in audit_path.read_text(encoding="utf-8")

        runtime.shutdown(reason="test")

        assert runtime.started is False
        assert runtime.worker.stop_event.is_set()
        assert harness.notifier.closed is True
        assert '"stopped"' in audit_path.read_text(encoding="utf-8")
        assert any("✅ kite-alerts started" in text for text in harness.notifier.texts)
        assert any("🛑 kite-alerts stopping (test)" in text for text in harness.notifier.texts)

    def test_health_status_pre_open_is_ok(self, tmp_path: Path) -> None:
        harness = build_harness(tmp_path)
        try:
            status = harness.runtime.health_status()
            assert status["healthy"] is True
            assert status["status"] == "ok"
            assert status["in_session"] is False
            assert status["service"] == "kite-alerts"
            assert "notifications" in status
            assert "engine" in status
        finally:
            harness.runtime.shutdown()

    def test_health_status_flags_a_silent_session(self, tmp_path: Path) -> None:
        harness = build_harness(tmp_path)
        try:
            runtime = harness.runtime
            harness.clock.set(pd.Timestamp("2026-11-16 09:30", tz="Asia/Kolkata"))
            status = runtime.health_status()
            assert status["in_session"] is True
            assert status["last_tick_age_seconds"] == 900.0
            assert status["healthy"] is False
            assert status["status"] == "stale_feed"

            runtime.worker.auth_failed = True
            assert runtime.health_status()["status"] == "auth_failed"
            assert runtime.health_status()["healthy"] is False
        finally:
            harness.runtime.shutdown()


class TestRunLiveFaults:
    def test_a_taken_health_port_exits_with_error(self, tmp_path: Path) -> None:
        harness = build_harness(tmp_path)
        runtime = harness.runtime
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        runtime.health = HealthServer(
            host="127.0.0.1",
            port=port,
            status_provider=runtime.health_status,
            metrics_provider=runtime.metrics.render_prometheus,
        )
        try:
            code = run_live(runtime.settings, runtime=runtime, install_signal_handlers=False)
        finally:
            blocker.close()
        assert code == EXIT_ERROR
        assert runtime.started is False

    def test_an_auth_error_at_start_exits_with_3(self, tmp_path: Path) -> None:
        harness = build_harness(tmp_path)
        runtime = harness.runtime
        runtime.hub.start()  # the real start() does this before anything else

        def refuse_to_start() -> None:
            raise AuthError("token expired at startup")

        runtime.start = refuse_to_start  # type: ignore[method-assign]
        code = run_live(runtime.settings, runtime=runtime, install_signal_handlers=False)

        assert code == EXIT_AUTH
        assert any("🔐 token expired at startup" in text for text in harness.notifier.texts)

    def test_a_loop_exception_exits_with_1_and_still_shuts_down(self, tmp_path: Path) -> None:
        harness = build_harness(tmp_path)
        runtime = harness.runtime

        def boom(timeout: float | None = None) -> bool:
            raise RuntimeError("run loop failure")

        runtime.worker.start = lambda: None  # type: ignore[method-assign]
        runtime.worker.stop = lambda timeout=10.0: None  # type: ignore[method-assign]
        runtime.worker.step = boom  # type: ignore[method-assign]

        code = run_live(runtime.settings, runtime=runtime, install_signal_handlers=False)

        assert code == EXIT_ERROR
        assert runtime.started is False
        assert harness.runtime.feed.connected is False

    def test_an_auth_failure_detected_earlier_wins_the_exit_code(self, tmp_path: Path) -> None:
        harness = build_harness(tmp_path)
        worker = harness.runtime.worker
        worker.auth_failed = True
        worker.stop_event.set()

        code = run_live(
            harness.runtime.settings,
            runtime=harness.runtime,
            install_signal_handlers=False,
        )

        assert code == EXIT_AUTH

    def test_the_configured_duration_stops_the_run(self, tmp_path: Path) -> None:
        harness = build_harness(tmp_path)
        runtime = harness.runtime

        code = run_live(
            runtime.settings,
            runtime=runtime,
            duration_seconds=0,
            install_signal_handlers=False,
        )

        assert code == EXIT_OK
        assert runtime.started is False

    def test_signal_handlers_set_the_stop_event_and_restore(self) -> None:
        import signal

        stop = threading.Event()
        previous = _install_signal_handlers(stop)
        try:
            if signal.SIGINT in previous:
                handler = signal.getsignal(signal.SIGINT)
                handler(signal.SIGINT, None)
                assert stop.is_set() is True
        finally:
            _restore_signal_handlers(previous)
        assert signal.getsignal(signal.SIGINT) == previous.get(signal.SIGINT)


class TestBuildRuntimeFaults:
    def test_a_missing_token_is_a_config_error(self, tmp_path: Path, warmup: pd.DataFrame) -> None:
        settings = live_settings(
            tmp_path,
            kite=KiteCredentials(api_key="key", api_secret=SecretStr("secret")),
        )
        with pytest.raises(ConfigError, match="KITE_ACCESS_TOKEN"):
            build_runtime(
                settings,
                kite=_kite(warmup),
                notifiers=[RecordingNotifier()],
                start_health=False,
            )

    def test_an_expired_token_fails_before_any_durable_state(
        self, tmp_path: Path, warmup: pd.DataFrame
    ) -> None:
        settings = live_settings(tmp_path)
        kite = FakeKite(profile_error=TokenException("expired"), instruments=[], history={})
        with pytest.raises(AuthError, match="rejected the access token"):
            build_runtime(settings, kite=kite, notifiers=[RecordingNotifier()], start_health=False)
        assert settings.db_path.exists() is False

    def test_no_notifier_is_a_config_error(self, tmp_path: Path, warmup: pd.DataFrame) -> None:
        settings = live_settings(tmp_path)
        with pytest.raises(ConfigError, match="no notification channels"):
            build_runtime(settings, kite=_kite(warmup), notifiers=[], start_health=False)

    def test_an_unknown_symbol_is_a_config_error(self, tmp_path: Path) -> None:
        settings = live_settings(tmp_path)
        kite = FakeKite(instruments=[], history={})
        with pytest.raises(ConfigError, match="none of the configured symbols"):
            build_runtime(settings, kite=kite, notifiers=[RecordingNotifier()], start_health=False)

    def test_warm_up_auth_failure_closes_the_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = live_settings(tmp_path)
        kite = FakeKite(
            instruments=[FakeInstrument(TOKEN, "NSE", "TEST", name="TEST")],
            history={},
        )
        kite.history_error = TokenException("expired")
        closed: list[bool] = []
        original_close = SqliteAlertStore.close

        def spy_close(self: SqliteAlertStore) -> None:
            closed.append(True)
            original_close(self)

        monkeypatch.setattr(SqliteAlertStore, "close", spy_close)
        with pytest.raises(AuthError, match="seeding history"):
            build_runtime(settings, kite=kite, notifiers=[RecordingNotifier()], start_health=False)
        assert closed == [True]

    def test_warm_up_network_failure_does_not_stop_startup(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = live_settings(tmp_path)
        kite = FakeKite(
            instruments=[FakeInstrument(TOKEN, "NSE", "TEST", name="TEST")],
            history={},
        )
        kite.history_error = ConnectionError("network down")
        runtime = build_runtime(
            settings, kite=kite, notifiers=[RecordingNotifier()], start_health=False
        )
        try:
            assert runtime.engine.snapshot() == {}
            assert "warm-up failed for NSE:TEST" in caplog.text
            assert "no history for NSE:TEST" in caplog.text
        finally:
            runtime.shutdown()
