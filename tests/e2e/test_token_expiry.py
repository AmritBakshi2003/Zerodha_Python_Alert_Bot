"""Expired token: fail fast at startup, alert once at runtime, stop cleanly."""

from __future__ import annotations

import pytest

from fakes.fake_kite import FakeTicker, RecordingNotifier, TokenException
from kite_alerts.cli.run import build_runtime
from kite_alerts.core.errors import AuthError

from .support import SESSION_DAY, build_harness, live_settings, make_fake_kite

pytestmark = pytest.mark.e2e


def test_startup_verification_fails_with_an_actionable_message(tmp_path) -> None:
    from kite_alerts.demo import synthetic_day

    warmup = synthetic_day().iloc[:50]
    fake = make_fake_kite(warmup, profile_error=TokenException("Incorrect api_key or access_token"))
    with pytest.raises(AuthError) as excinfo:
        build_runtime(
            live_settings(tmp_path),
            kite=fake,
            ticker_factory=FakeTicker,
            notifiers=[RecordingNotifier()],
            start_health=False,
        )
    assert "kite-alerts login" in str(excinfo.value)


def test_backfill_auth_failure_alerts_and_stops(tmp_path) -> None:
    harness = build_harness(tmp_path)
    try:
        harness.clock.set("2026-11-16 09:16:00")  # back-fill only runs during a session
        harness.fake.history_error = TokenException("access token expired")
        harness.start_feed()
        harness.step()  # processes the back-fill command
        harness.runtime.hub.flush(timeout=5)

        assert harness.runtime.worker.auth_failed is True
        assert harness.runtime.worker.stop_event.is_set() is True
        assert any("kite-alerts login" in text for text in harness.notifier.texts)
        assert any(text.startswith("🔐") for text in harness.notifier.texts)
    finally:
        harness.runtime.shutdown()


def test_websocket_auth_error_is_detected(tmp_path) -> None:
    harness = build_harness(tmp_path)
    try:
        harness.clock.set("2026-11-16 09:16:00")
        ticker = harness.start_feed()
        harness.step()
        ticker.error(403, "TokenException: token is invalid")
        harness.runtime.hub.flush(timeout=5)

        assert harness.runtime.worker.auth_failed is True
        assert any("kite-alerts login" in text for text in harness.notifier.texts)
    finally:
        harness.runtime.shutdown()


def test_auth_failure_is_reported_once(tmp_path) -> None:
    harness = build_harness(tmp_path)
    try:
        harness.clock.set("2026-11-16 09:16:00")
        ticker = harness.start_feed()
        harness.step()
        ticker.error(403, "TokenException")
        ticker.error(403, "TokenException")
        harness.runtime.hub.flush(timeout=5)
        auth_messages = [text for text in harness.notifier.texts if text.startswith("🔐")]
        assert len(auth_messages) == 1
    finally:
        harness.runtime.shutdown()


def test_run_live_returns_the_auth_exit_code(tmp_path) -> None:
    from kite_alerts.cli.run import EXIT_AUTH, run_live

    harness = build_harness(tmp_path)
    harness.clock.set("2026-11-16 09:16:00")
    harness.fake.history_error = TokenException("expired")
    try:
        code = run_live(
            harness.runtime.settings,
            runtime=harness.runtime,
            duration_seconds=0.3,
            install_signal_handlers=False,
        )
    finally:
        if harness.runtime.started:
            harness.runtime.shutdown()
    assert code == EXIT_AUTH


def test_session_date_is_the_expected_trading_day() -> None:
    import datetime as dt

    assert dt.date.fromisoformat(SESSION_DAY).weekday() == 0
