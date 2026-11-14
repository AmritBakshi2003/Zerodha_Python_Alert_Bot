"""Dashboard API: health states, history, demo, test alert and secret safety."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kite_alerts.config.settings import load_settings
from kite_alerts.core.models import Side
from kite_alerts.notifiers.base import DeliveryError
from kite_alerts.ui.app import build_context, create_app
from kite_alerts.ui.history import EMPTY_MESSAGE, MISSING_TABLE_MESSAGE, UNREADABLE_MESSAGE
from unit.ui_support import (
    BASE_URL,
    TODAY,
    FakeMonotonic,
    FakeNotifierFactory,
    FixedClock,
    make_alert,
    make_client,
    make_context,
    make_settings,
    seed_alerts,
)

GOLDEN_LINES = [
    line
    for line in (Path(__file__).resolve().parents[2] / "demo_output.txt")
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip() and not line.startswith("3 alert(s)")
]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alerts.db"


@pytest.fixture
def settings(db_path: Path) -> Any:
    return make_settings(db_path)


@pytest.fixture
def client(settings: Any) -> Iterator[TestClient]:
    context = make_context(settings, notifier_factory=FakeNotifierFactory([("console", None)]))
    with make_client(context) as test_client:
        yield test_client


def status_of(probe: Any, *, http_enabled: bool = True, db: Path, use_settings: bool = True) -> Any:
    settings = make_settings(db, http_enabled=http_enabled) if use_settings else None
    context = make_context(settings, db_path=db, probe=probe)
    with make_client(context) as test_client:
        return test_client.get("/api/status").json()


class TestStatus:
    def test_not_running_when_nothing_answers(self, db_path: Path) -> None:
        data = status_of(lambda _url, _timeout: None, db=db_path)
        assert data["status"]["running"] is False
        assert data["status"]["label"] == "Bot not running"
        assert "kite-alerts run" in data["status"]["detail"]
        assert data["status"]["endpoint"] == "http://127.0.0.1:9109/health"
        assert data["alerts_only"] is True

    def test_running_when_health_is_ok(self, db_path: Path) -> None:
        data = status_of(lambda _url, _timeout: (200, {"status": "ok"}), db=db_path)
        assert data["status"]["running"] is True
        assert data["status"]["label"] == "Bot running"
        assert data["status"]["health_status"] == "ok"

    def test_running_but_unhealthy_still_counts_as_running(self, db_path: Path) -> None:
        data = status_of(lambda _url, _timeout: (503, {"status": "stale_feed"}), db=db_path)
        assert data["status"]["running"] is True
        assert data["status"]["label"] == "Bot running"
        assert "stale_feed" in data["status"]["detail"]

    def test_probe_exception_is_not_a_crash(self, db_path: Path) -> None:
        def broken(_url: str, _timeout: float) -> Any:
            raise RuntimeError("boom")

        data = status_of(broken, db=db_path)
        assert data["status"]["running"] is False

    def test_disabled_health_endpoint(self, db_path: Path) -> None:
        data = status_of(lambda _url, _timeout: (200, {}), http_enabled=False, db=db_path)
        assert data["status"]["running"] is False
        assert "HTTP_ENABLED" in data["status"]["detail"]

    def test_without_settings(self, db_path: Path) -> None:
        data = status_of(lambda _url, _timeout: None, db=db_path, use_settings=False)
        assert data["status"]["running"] is False
        assert "could not read your settings" in data["status"]["detail"]


class TestHistory:
    def test_empty_store_has_a_friendly_empty_state(self, client: TestClient) -> None:
        data = client.get("/api/alerts/history").json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["pages"] == 1
        assert data["message"] == EMPTY_MESSAGE
        assert data["available"] is True

    def test_populated_history_with_full_reason(self, db_path: Path, client: TestClient) -> None:
        seed_alerts(
            db_path,
            [
                make_alert(
                    side=Side.SELL,
                    when=f"{TODAY} 09:34",
                    price=99.8,
                    reasons=("RSI 40.9 in bearish zone", "MACD below signal, histogram falling"),
                )
            ],
        )
        data = client.get("/api/alerts/history").json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["side"] == "SELL"
        assert item["time"] == "09:34"
        assert item["reasons"] == [
            "RSI 40.9 in bearish zone",
            "MACD below signal, histogram falling",
        ]
        assert item["message"] == (
            "🔴 SELL NSE:TEST @ 99.80 [09:34] — RSI 40.9 in bearish zone; "
            "MACD below signal, histogram falling"
        )

    def test_filters_and_pagination(self, db_path: Path, client: TestClient) -> None:
        seed_alerts(
            db_path,
            [
                make_alert(symbol="NSE:AAA", side=Side.SELL, when=f"{TODAY} 09:34"),
                make_alert(symbol="NSE:AAA", side=Side.SELL, when=f"{TODAY} 09:49"),
                make_alert(symbol="NSE:AAA", side=Side.BUY, when=f"{TODAY} 10:07"),
                make_alert(symbol="NSE:BBB", side=Side.BUY, when=f"{TODAY} 10:30"),
            ],
        )
        page_one = client.get("/api/alerts/history", params={"page_size": 2}).json()
        assert (page_one["total"], page_one["pages"], len(page_one["items"])) == (4, 2, 2)

        page_two = client.get("/api/alerts/history", params={"page_size": 2, "page": 2}).json()
        assert (page_two["page"], len(page_two["items"])) == (2, 2)

        filtered = client.get("/api/alerts/history", params={"symbol": "nse:aaa"}).json()
        assert filtered["total"] == 3
        assert {item["symbol"] for item in filtered["items"]} == {"NSE:AAA"}
        assert filtered["symbols"] == ["NSE:AAA", "NSE:BBB"]

        sells = client.get("/api/alerts/history", params={"side": "SELL"}).json()
        assert sells["total"] == 2
        assert all(item["side"] == "SELL" for item in sells["items"])

    def test_invalid_side_is_a_plain_422(self, client: TestClient) -> None:
        response = client.get("/api/alerts/history", params={"side": "HOLD"})
        assert response.status_code == 422
        assert "BUY or SELL" in response.json()["detail"]

    def test_database_is_opened_read_only(self, db_path: Path, client: TestClient) -> None:
        seed_alerts(db_path, [make_alert()])
        reader = make_context(make_settings(db_path)).history
        assert "mode=ro" in reader.db_uri
        connection = reader.open_read_only()
        assert connection is not None
        try:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("DELETE FROM alerts")
        finally:
            connection.close()
        # The failed write and the API traffic must leave the row intact.
        assert client.get("/api/alerts/history").json()["total"] == 1

    def test_missing_file_is_empty_not_an_error(self, tmp_path: Path) -> None:
        context = make_context(make_settings(tmp_path / "absent.db"))
        with make_client(context) as test_client:
            data = test_client.get("/api/alerts/history").json()
        assert data["items"] == []
        assert data["message"] == EMPTY_MESSAGE

    def test_database_without_alerts_table(self, tmp_path: Path) -> None:
        path = tmp_path / "other.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE notes (x TEXT)")
        connection.commit()
        connection.close()
        context = make_context(make_settings(path))
        with make_client(context) as test_client:
            data = test_client.get("/api/alerts/history").json()
        assert data["available"] is False
        assert data["message"] == MISSING_TABLE_MESSAGE

    def test_corrupt_database_is_reported_plainly(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.db"
        path.write_bytes(b"this is not a sqlite database")
        context = make_context(make_settings(path))
        with make_client(context) as test_client:
            data = test_client.get("/api/alerts/history").json()
        assert data["available"] is False
        assert data["message"] == UNREADABLE_MESSAGE

    def test_today_counts_use_ist_date(self, db_path: Path) -> None:
        seed_alerts(
            db_path,
            [
                make_alert(side=Side.BUY, when=f"{TODAY} 09:20"),
                make_alert(side=Side.BUY, when=f"{TODAY} 10:20", symbol="NSE:BBB"),
                make_alert(side=Side.SELL, when=f"{TODAY} 11:20"),
                make_alert(side=Side.SELL, when="2026-11-15 11:20"),
            ],
        )
        context = make_context(make_settings(db_path), history_clock=FixedClock(f"{TODAY} 15:00"))
        with make_client(context) as test_client:
            data = test_client.get("/api/status").json()
        assert data["today"] == {
            "date": TODAY,
            "buys": 2,
            "sells": 1,
            "total": 3,
            "available": True,
        }

    def test_recent_feed_is_newest_first(self, db_path: Path, client: TestClient) -> None:
        seed_alerts(
            db_path,
            [
                make_alert(when=f"{TODAY} 09:34"),
                make_alert(side=Side.SELL, when=f"{TODAY} 09:49"),
                make_alert(when=f"{TODAY} 10:07"),
            ],
        )
        data = client.get("/api/alerts/recent", params={"limit": 2}).json()
        assert len(data["items"]) == 2
        assert [item["time"] for item in data["items"]] == ["10:07", "09:49"]
        assert data["message"] is None


class TestDemo:
    def test_timeline_has_three_demo_alerts(self, client: TestClient) -> None:
        response = client.get("/api/demo/timeline")
        assert response.status_code == 200
        data = response.json()
        assert data["demo"] is True
        assert data["label"] == "DEMO"
        assert data["counts"] == {"buys": 1, "sells": 2, "total": 3}
        assert [alert["message"] for alert in data["alerts"]] == GOLDEN_LINES
        assert [alert["side"] for alert in data["alerts"]] == ["SELL", "SELL", "BUY"]
        assert len(data["candles"]) == 151
        assert data["orb"] is not None

    def test_stream_yields_the_same_alerts(self, client: TestClient) -> None:
        response = client.get("/api/demo/stream")
        assert response.status_code == 200
        assert "application/x-ndjson" in response.headers["content-type"]
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        alerts = [event for event in events if event["type"] == "alert"]
        assert [event["alert"]["message"] for event in alerts] == GOLDEN_LINES
        assert events[-1] == {
            "type": "end",
            "demo": True,
            "counts": {"buys": 1, "sells": 2, "total": 3},
        }

    def test_demo_never_writes_to_the_real_store(self, db_path: Path) -> None:
        seed_alerts(db_path, [make_alert()])
        context = make_context(make_settings(db_path))
        with make_client(context) as test_client:
            assert test_client.get("/api/demo/timeline").status_code == 200
            assert test_client.get("/api/demo/stream").status_code == 200
            history = test_client.get("/api/alerts/history").json()
        assert history["total"] == 1
        assert history["items"][0]["symbol"] == "NSE:TEST"


class TestAlertEndpoint:
    def test_rate_limited_to_one_per_ten_seconds(self, db_path: Path) -> None:
        clock = FakeMonotonic()
        factory = FakeNotifierFactory([("console", None)])
        context = make_context(make_settings(db_path), notifier_factory=factory, test_clock=clock)
        with make_client(context) as client:
            first = client.post("/api/test-alert")
            assert first.status_code == 200
            assert first.json()["ok"] is True
            assert factory.created[0].sent == 1

            second = client.post("/api/test-alert")
            assert second.status_code == 429
            assert second.headers["retry-after"] == "10"
            assert "Try again in 10 seconds" in second.json()["detail"]
            assert factory.created[0].sent == 1

            clock.advance(10.0)
            third = client.post("/api/test-alert")
            assert third.status_code == 200

    def test_console_only_explains_how_to_add_channels(self, db_path: Path) -> None:
        factory = FakeNotifierFactory([("console", None)])
        context = make_context(make_settings(db_path), notifier_factory=factory)
        with make_client(context) as client:
            data = client.post("/api/test-alert").json()
        assert data["ok"] is True
        assert "Only the console channel" in data["detail"]
        assert len(data["setup"]) == 2
        assert "TELEGRAM_BOT_TOKEN" in data["setup"][0]
        assert "SLACK_WEBHOOK_URL" in data["setup"][1]

    def test_per_channel_failure_is_plain_and_redacted(self, db_path: Path) -> None:
        error = DeliveryError(
            "slack",
            "HTTP 500 for https://hooks.slack.com/services/T00/B00/SECRETPART",
        )
        factory = FakeNotifierFactory([("console", None), ("slack", error)])
        context = make_context(make_settings(db_path), notifier_factory=factory)
        with make_client(context) as client:
            response = client.post("/api/test-alert")
        data = response.json()
        assert response.status_code == 200
        assert data["ok"] is False
        assert [result["channel"] for result in data["results"]] == ["console", "slack"]
        assert data["results"][0]["ok"] is True
        assert data["results"][1]["ok"] is False
        assert "could not send" in data["results"][1]["message"]
        assert "hooks.slack.com/services/<redacted>" in data["results"][1]["message"]
        assert "SECRETPART" not in response.text

    def test_without_settings_returns_503(self, db_path: Path) -> None:
        context = make_context(
            None,
            db_path=db_path,
            settings_error="invalid configuration:\n  - EMA_FAST: not an integer",
        )
        with make_client(context) as client:
            response = client.post("/api/test-alert")
        assert response.status_code == 503
        assert "could not be read" in response.json()["detail"]


class TestAboutAndSecrets:
    def test_about_reports_version_mode_and_checklist(self, client: TestClient) -> None:
        data = client.get("/api/about").json()
        assert data["version"]
        assert data["alerts_only"] is True
        assert "never places" in data["alerts_only_statement"]
        assert len(data["checklist"]) >= 4
        assert data["attribution"]["license"] == "Apache-2.0"
        assert "TradingView" in data["attribution"]["notice"]
        assert data["attribution"]["license_url"] == "/license"

    def test_no_endpoint_returns_a_credential(self, tmp_path: Path) -> None:
        markers = {
            "KITE_API_KEY": "KEY-MARKER-4f1a",
            "KITE_API_SECRET": "SECRET-MARKER-9c2b",
            "KITE_ACCESS_TOKEN": "TOKEN-MARKER-77d0",
            "TELEGRAM_BOT_TOKEN": "123456:TELEGRAM-MARKER-abc",
            "TELEGRAM_CHAT_ID": "CHAT-MARKER-555",
            "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/SLACK-MARKER-zzz",
            "NOTIFY_CHANNELS": "console,telegram,slack",
            "SYMBOLS": "NSE:TEST",
        }
        settings = load_settings(tmp_path / "absent.env", environ=markers)
        factory = FakeNotifierFactory(
            [
                ("console", None),
                ("telegram", DeliveryError("telegram", "HTTP 401 from Telegram")),
            ]
        )
        context = build_context(
            env_file=tmp_path / "absent.env",
            db_path=tmp_path / "alerts.db",
            notifier_factory=factory,
        )
        assert context.settings is not None
        app = create_app(context)
        with TestClient(app, base_url=BASE_URL) as client:
            paths = [
                "/",
                "/license",
                "/notice",
                "/api/about",
                "/api/status",
                "/api/alerts/recent",
                "/api/alerts/history",
                "/api/demo/timeline",
                "/api/demo/stream",
                "/static/index.html",
                "/static/app.js",
                "/static/styles.css",
                "/static/favicon.svg",
                "/static/vendor/lightweight-charts/lightweight-charts.standalone.production.js",
            ]
            responses = [client.get(path) for path in paths]
            responses.append(client.post("/api/test-alert"))
            for response in responses:
                assert response.status_code < 500, response.text
                text = response.text
                headers = "\n".join(f"{key}: {value}" for key, value in response.headers.items())
                for marker in markers.values():
                    assert marker not in text, f"secret leaked by {response.request.url}"
                    assert marker not in headers
        assert settings.kite.api_key == markers["KITE_API_KEY"]  # the app did load it
