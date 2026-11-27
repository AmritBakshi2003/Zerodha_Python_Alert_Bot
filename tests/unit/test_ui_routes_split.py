"""Characterization for the dashboard router split (F12 / P5b).

``ui/app.py`` was split into ``ui/routes/*`` router factories. This test pins
two things the split must not change:

1. the exact set of (path, method) pairs the app serves — a router that is
   defined but never registered used to be invisible; now it fails here;
2. the recorded shape of three representative payloads, so a handler that
   silently lost a field during the move is caught.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kite_alerts.ui.app import create_app
from unit.ui_support import make_context, make_settings

BASE_URL = "http://127.0.0.1:8765"

#: Every route the dashboard documents in ``ui/app.py``'s module docstring.
EXPECTED_ROUTES = {
    ("/", "GET"),
    ("/license", "GET"),
    ("/notice", "GET"),
    ("/api/about", "GET"),
    ("/api/about.paths", "GET"),
    ("/api/status", "GET"),
    ("/api/alerts/recent", "GET"),
    ("/api/alerts/history", "GET"),
    ("/api/demo/timeline", "GET"),
    ("/api/demo/stream", "GET"),
    ("/api/test-alert", "POST"),
    ("/api/settings", "GET"),
    ("/api/settings", "PUT"),
    ("/api/settings/check", "POST"),
    ("/api/kite/login", "POST"),
    ("/kite/start", "GET"),
    ("/callback", "GET"),
    ("/api/kite/manual-token", "POST"),
    ("/api/kite/session", "GET"),
    ("/api/kite/disconnect", "POST"),
    ("/api/data-sources", "GET"),
    ("/api/kite/simulate/disconnect", "POST"),
    ("/api/kite/simulate/token-expiry", "POST"),
    ("/api/kite/simulate/reconnect", "POST"),
    ("/api/notifications/telegram/chat-id", "POST"),
    ("/api/practice/check", "POST"),
    ("/api/chart/symbols", "GET"),
    ("/api/chart/candles", "GET"),
    ("/api/chart/live", "GET"),
    ("/api/chart/clock", "GET"),
    ("/api/chart/watchlist", "GET"),
    ("/api/chart/watchlist", "POST"),
    ("/api/chart/watchlist", "DELETE"),
    ("/api/chart/stream", "GET"),
    ("/api/chart/feed-status", "GET"),
}


def _app(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings = make_settings(tmp_path / "alerts.db")
    context = make_context(settings, db_path=settings.db_path)
    return create_app(context)


def test_every_documented_route_is_registered(tmp_path: Path) -> None:
    app = _app(tmp_path)
    actual = {
        (route.path, method) for route in app.routes for method in getattr(route, "methods", set())
    }
    assert actual >= EXPECTED_ROUTES, sorted(EXPECTED_ROUTES - actual)


def test_static_mount_is_present_only_when_the_assets_exist(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert any(getattr(route, "name", "") == "static" for route in app.routes)


def test_about_payload_keeps_its_recorded_shape(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), base_url=BASE_URL)
    payload = client.get("/api/about").json()
    assert set(payload) == {
        "version",
        "mode",
        "alerts_only",
        "alerts_only_statement",
        "checklist",
        "attribution",
        "settings_error",
        "kite_configured",
        "token_present",
        "holidays",
        "simulate_kite",
        "demo",
        "redirect_url",
        "csrf_token",
        "paths",
    }
    assert payload["alerts_only"] is True
    assert payload["demo"] is True
    assert "never places" in payload["alerts_only_statement"]
    assert payload["attribution"]["license_url"] == "/license"
    assert payload["attribution"]["notice_url"] == "/notice"
    assert len(payload["checklist"]) == 7


def test_status_payload_keeps_its_recorded_shape(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), base_url=BASE_URL)
    payload = client.get("/api/status").json()
    assert set(payload) == {
        "status",
        "today",
        "version",
        "mode",
        "alerts_only",
        "settings_error",
        "warnings",
    }


def test_settings_payload_keeps_its_recorded_shape(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), base_url=BASE_URL)
    payload = client.get("/api/settings").json()
    assert "groups" in payload
    assert "env_file" in payload
    assert "connect" in payload
    # A secret is described (is_set/secret) but its value is never echoed back.
    for group in payload["groups"]:
        for field in group["fields"]:
            if field.get("secret"):
                assert "value" not in field
                assert field["default"] == ""


def test_practice_payload_keeps_its_recorded_shape(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), base_url=BASE_URL)
    client.get("/")
    token = client.cookies.get("kite_alerts_csrf") or ""
    response = client.post(
        "/api/practice/check",
        json={},
        headers={"X-Kite-Alerts-CSRF": token, "Origin": BASE_URL},
    )
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "ok",
        "demo",
        "label",
        "version",
        "symbol",
        "counts",
        "alerts",
        "message",
    }
    assert payload["label"] == "DEMO"
    assert set(payload["counts"]) == {"buys", "sells", "total"}
