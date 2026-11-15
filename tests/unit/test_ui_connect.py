"""The bring-your-own-key Kite connect flow, entirely offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kite_alerts.ui.kite_connect import STATE_COOKIE
from unit.ui_support import BASE_URL, FakeMonotonic
from unit.ui_support_c2 import (
    ACCESS_MARKER,
    BASE_ENV,
    KEY_MARKER,
    REQUEST_MARKER,
    SECRET_MARKER,
    TOKEN_MARKER,
    FakeKite,
    client_for,
    connect_client,
    env_text_with_token,
)


class TokenException(Exception):
    pass


class PermissionException(Exception):
    pass


class NetworkException(Exception):
    pass


class RateException(Exception):
    def __init__(self) -> None:
        super().__init__("too many requests")
        self.code = 429


@pytest.fixture
def ui(tmp_path: Path):
    kite = FakeKite()
    client, context, env_path = client_for(tmp_path, kite=kite)
    with client:
        yield client, context, env_path, kite


def start_login(client: TestClient) -> dict:
    response = client.post("/api/kite/login")
    assert response.status_code == 200, response.text
    return response.json()


class TestStart:
    def test_login_returns_a_relative_start_path_not_the_key(self, ui) -> None:
        client, _context, _env, kite = ui
        response = client.post("/api/kite/login")
        assert response.status_code == 200
        body = response.json()
        assert body["start_url"] == "/kite/start"
        assert body["redirect_url"] == "http://127.0.0.1:8765/callback"
        assert body["expires_in"] == 600
        assert KEY_MARKER not in response.text
        cookie = response.headers["set-cookie"]
        assert STATE_COOKIE in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie.lower().replace("samesite=lax", "SameSite=lax")
        assert "login_url" in kite.calls

    def test_start_path_redirects_to_zerodha(self, ui) -> None:
        client, _context, _env, _kite = ui
        start_login(client)
        response = client.get("/kite/start", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"].startswith("https://kite.example/connect/login")

    def test_start_without_a_redirect_attempt_is_a_clear_failure(self, ui) -> None:
        client, _context, _env, _kite = ui
        response = client.get("/kite/start", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/?connect=no_attempt#/connect"

    def test_missing_key_is_refused_with_guidance(self, tmp_path: Path) -> None:
        client, _context, _env, _kite = connect_client(
            tmp_path,
            env_text="KITE_API_SECRET=SECRET-MARKER-9c2b\nSYMBOLS=NSE:TEST\n",
        )
        with client:
            response = client.post("/api/kite/login")
        assert response.status_code == 400
        assert response.json()["code"] == "missing_key"
        assert "developers.kite.trade" in response.json()["message"]

    def test_missing_secret_is_refused(self, tmp_path: Path) -> None:
        client, _context, _env, _kite = connect_client(
            tmp_path,
            env_text="KITE_API_KEY=KEY-MARKER-4f1a\nSYMBOLS=NSE:TEST\n",
        )
        with client:
            response = client.post("/api/kite/login")
        assert response.status_code == 400
        assert response.json()["code"] == "missing_secret"

    def test_broken_settings_cannot_start_a_login(self, tmp_path: Path) -> None:
        client, context, _env, _kite = connect_client(
            tmp_path, env_text=BASE_ENV + "EMA_FAST=oops\n"
        )
        assert context.settings is None
        with client:
            response = client.post("/api/kite/login")
        assert response.status_code == 400
        assert response.json()["code"] == "settings"

    def test_redirect_url_follows_the_running_port(self, tmp_path: Path) -> None:
        client, _context, _env, _kite = connect_client(tmp_path, port=8899)
        with client:
            body = client.post("/api/kite/login").json()
        assert body["redirect_url"] == "http://127.0.0.1:8899/callback"


class TestCallback:
    def test_full_loopback_flow_saves_the_token(self, ui) -> None:
        client, context, env_path, kite = ui
        start_login(client)
        response = client.get(f"/callback?request_token={REQUEST_MARKER}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/?connect=connected#/connect"
        assert "generate_session" in kite.calls
        assert kite.last_request_token == REQUEST_MARKER
        assert kite.last_secret == SECRET_MARKER
        text = env_path.read_text(encoding="utf-8")
        assert f"KITE_ACCESS_TOKEN={ACCESS_MARKER}" in text
        assert context.settings is not None
        assert context.settings.kite.token == ACCESS_MARKER
        session = client.get("/api/kite/session").json()
        assert session["status"] == "connected"
        assert session["name"] == "Demo Trader"
        assert ACCESS_MARKER not in json.dumps(session)

    def test_the_attempt_is_single_use(self, ui) -> None:
        client, _context, _env, _kite = ui
        start_login(client)
        client.get(f"/callback?request_token={REQUEST_MARKER}", follow_redirects=False)
        second = client.get(f"/callback?request_token={REQUEST_MARKER}", follow_redirects=False)
        assert second.headers["location"] == "/?connect=no_attempt#/connect"

    def test_without_any_attempt_the_callback_is_refused(self, ui) -> None:
        client, _context, _env, _kite = ui
        response = client.get(f"/callback?request_token={REQUEST_MARKER}", follow_redirects=False)
        assert response.headers["location"] == "/?connect=no_attempt#/connect"

    def test_a_wrong_state_is_refused(self, ui) -> None:
        client, _context, env_path, _kite = ui
        start_login(client)
        response = client.get(
            f"/callback?request_token={REQUEST_MARKER}&state=wrong",
            follow_redirects=False,
        )
        assert response.headers["location"] == "/?connect=bad_state#/connect"
        assert "KITE_ACCESS_TOKEN" not in env_path.read_text(encoding="utf-8")

    def test_a_missing_state_is_refused(self, ui) -> None:
        client, context, _env, _kite = ui
        start_login(client)
        client.cookies.clear()
        response = client.get(f"/callback?request_token={REQUEST_MARKER}", follow_redirects=False)
        assert response.headers["location"] == "/?connect=bad_state#/connect"
        assert context.settings is not None
        assert context.settings.kite.token == ""

    def test_an_expired_attempt_is_refused(self, ui) -> None:
        client, context, _env, _kite = ui
        clock = FakeMonotonic()
        assert context.kite is not None
        context.kite.monotonic = clock
        start_login(client)
        clock.advance(601)
        response = client.get(f"/callback?request_token={REQUEST_MARKER}", follow_redirects=False)
        assert response.headers["location"] == "/?connect=state_expired#/connect"

    def test_a_cancelled_login_changes_nothing(self, ui) -> None:
        client, _context, env_path, _kite = ui
        start_login(client)
        response = client.get("/callback?status=cancelled", follow_redirects=False)
        assert response.headers["location"] == "/?connect=cancelled#/connect"
        assert "KITE_ACCESS_TOKEN" not in env_path.read_text(encoding="utf-8")

    def test_a_rejected_token_is_explained_without_echoing_it(self, ui) -> None:
        client, _context, env_path, kite = ui
        kite.session_error = TokenException(f"rejected {REQUEST_MARKER}")
        start_login(client)
        response = client.get(f"/callback?request_token={REQUEST_MARKER}", follow_redirects=False)
        assert response.headers["location"] == "/?connect=exchange_failed#/connect"
        assert "KITE_ACCESS_TOKEN" not in env_path.read_text(encoding="utf-8")

    def test_an_empty_session_response_is_a_failure(self, ui) -> None:
        client, _context, _env, kite = ui
        kite.access_token = ""
        start_login(client)
        response = client.get(f"/callback?request_token={REQUEST_MARKER}", follow_redirects=False)
        assert response.headers["location"] == "/?connect=exchange_failed#/connect"


class TestManualFallback:
    def test_manual_token_finishes_the_connection(self, ui) -> None:
        client, _context, env_path, _kite = ui
        start_login(client)
        response = client.post("/api/kite/manual-token", json={"request_token": REQUEST_MARKER})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["code"] == "connected"
        assert REQUEST_MARKER not in response.text
        assert f"KITE_ACCESS_TOKEN={ACCESS_MARKER}" in env_path.read_text(encoding="utf-8")

    def test_manual_token_requires_a_pending_attempt(self, ui) -> None:
        client, _context, _env, _kite = ui
        response = client.post("/api/kite/manual-token", json={"request_token": REQUEST_MARKER})
        assert response.status_code == 400
        assert response.json()["code"] == "no_attempt"

    def test_manual_token_rejects_a_wrong_state(self, ui) -> None:
        client, _context, _env, _kite = ui
        start_login(client)
        response = client.post(
            "/api/kite/manual-token",
            json={"request_token": REQUEST_MARKER, "state": "wrong"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "bad_state"

    def test_manual_token_requires_a_value(self, ui) -> None:
        client, _context, _env, _kite = ui
        start_login(client)
        response = client.post("/api/kite/manual-token", json={"request_token": ""})
        assert response.status_code == 400
        assert response.json()["code"] == "missing_token"


class TestSession:
    def test_profile_is_cached_for_a_minute(self, ui) -> None:
        client, _context, _env, kite = ui
        start_login(client)
        client.get(f"/callback?request_token={REQUEST_MARKER}", follow_redirects=False)
        before = kite.calls.count("profile")
        assert client.get("/api/kite/session").status_code == 200
        assert client.get("/api/kite/session").status_code == 200
        assert kite.calls.count("profile") == before

    def test_an_expired_token_is_reported(self, tmp_path: Path) -> None:
        kite = FakeKite(profile_error=TokenException("expired"))
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(), kite=kite
        )
        with client:
            body = client.get("/api/kite/session").json()
        assert body["status"] == "expired"
        assert body["connected"] is False
        assert "Reconnect" in body["message"]

    def test_an_unreachable_kite_is_reported(self, tmp_path: Path) -> None:
        kite = FakeKite(profile_error=NetworkException("no route"))
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(), kite=kite
        )
        with client:
            body = client.get("/api/kite/session").json()
        assert body["status"] == "unreachable"
        assert "unreachable" in body["message"]

    def test_an_unexpected_error_does_not_claim_a_connection(self, tmp_path: Path) -> None:
        kite = FakeKite(profile_error=ValueError("weird"))
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(), kite=kite
        )
        with client:
            body = client.get("/api/kite/session").json()
        assert body["status"] == "not_connected"
        assert body["connected"] is False

    def test_no_token_means_not_connected(self, ui) -> None:
        client, _context, _env, _kite = ui
        body = client.get("/api/kite/session").json()
        assert body["status"] == "not_connected"
        assert body["expires_note"] == "expires about 06:00 IST"
        assert body["credentials"]["fields"][1]["is_set"] is True

    def test_credentials_are_only_reported_as_set(self, ui) -> None:
        client, _context, _env, _kite = ui
        response = client.get("/api/kite/session")
        assert KEY_MARKER not in response.text
        assert SECRET_MARKER not in response.text
        for field in response.json()["credentials"]["fields"]:
            if field["secret"]:
                assert "value" not in field

    def test_session_survives_broken_settings(self, tmp_path: Path) -> None:
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=BASE_ENV + "EMA_FAST=oops\n"
        )
        with client:
            body = client.get("/api/kite/session").json()
        assert body["status"] == "not_connected"
        assert "settings" in body["message"]


class TestDisconnect:
    def test_disconnect_clears_the_saved_token(self, tmp_path: Path) -> None:
        kite = FakeKite()
        client, _context, env_path, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(token=TOKEN_MARKER), kite=kite
        )
        with client:
            response = client.post("/api/kite/disconnect")
            assert response.status_code == 200
            assert response.json()["code"] == "disconnected"
            assert "invalidate_access_token" in kite.calls
            assert "KITE_ACCESS_TOKEN=\n" in env_path.read_text(encoding="utf-8")
            assert client.get("/api/kite/session").json()["status"] == "not_connected"
            second = client.post("/api/kite/disconnect")
            assert "Already disconnected" in second.json()["message"]

    def test_invalidation_failure_still_clears_locally(self, tmp_path: Path) -> None:
        kite = FakeKite(invalidate_error=NetworkException("offline"))
        client, _context, env_path, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(), kite=kite
        )
        with client:
            assert client.post("/api/kite/disconnect").status_code == 200
        assert "KITE_ACCESS_TOKEN=\n" in env_path.read_text(encoding="utf-8")


class TestDataSources:
    def test_without_a_token_live_data_is_unavailable(self, ui) -> None:
        client, _context, _env, _kite = ui
        body = client.get("/api/data-sources").json()
        live = next(source for source in body["sources"] if source["id"] == "kite_live")
        assert live["available"] is False
        assert "Connect to Zerodha" in live["reason"]
        assert {source["id"] for source in body["sources"]} == {
            "nse_delayed",
            "kite_live",
            "crypto_showcase",
            "demo",
        }
        assert all(source["available"] for source in body["sources"] if source["id"] != "kite_live")

    def test_live_data_is_available_on_success(self, tmp_path: Path) -> None:
        kite = FakeKite(ltp_result={"NSE:NIFTY 50": {"last_price": 1}})
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(), kite=kite
        )
        with client:
            body = client.get("/api/data-sources").json()
        live = next(source for source in body["sources"] if source["id"] == "kite_live")
        assert live["available"] is True
        assert "Live data available" in live["reason"]
        assert body["checked_at"]

    def test_no_market_data_plan_is_explained(self, tmp_path: Path) -> None:
        kite = FakeKite(ltp_error=PermissionException("insufficient permission"))
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(), kite=kite
        )
        with client:
            body = client.get("/api/data-sources").json()
        live = next(source for source in body["sources"] if source["id"] == "kite_live")
        assert live["available"] is False
        assert "no market data" in live["reason"]
        assert "delayed chart" in live["reason"]

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (TokenException("expired"), "Token expired"),
            (NetworkException("offline"), "unreachable"),
            (RateException(), "rate limiting"),
            (ValueError("odd"), "could not confirm"),
        ],
    )
    def test_probe_errors_are_plain_messages(
        self, tmp_path: Path, error: BaseException, expected: str
    ) -> None:
        kite = FakeKite(ltp_error=error)
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(), kite=kite
        )
        with client:
            body = client.get("/api/data-sources").json()
        live = next(source for source in body["sources"] if source["id"] == "kite_live")
        assert live["available"] is False
        assert expected in live["reason"]

    def test_an_empty_probe_is_unavailable(self, tmp_path: Path) -> None:
        kite = FakeKite(ltp_result={})
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(), kite=kite
        )
        with client:
            body = client.get("/api/data-sources").json()
        live = next(source for source in body["sources"] if source["id"] == "kite_live")
        assert live["available"] is False
        assert "no data" in live["reason"]

    def test_the_probe_is_cached_until_refreshed(self, tmp_path: Path) -> None:
        kite = FakeKite()
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(), kite=kite
        )
        with client:
            client.get("/api/data-sources")
            client.get("/api/data-sources")
            assert kite.calls.count("ltp") == 1
            client.get("/api/data-sources", params={"refresh": True})
            assert kite.calls.count("ltp") == 2

    def test_a_rejected_plan_probe_does_not_break_the_page(self, tmp_path: Path) -> None:
        kite = FakeKite(ltp_error=RuntimeError("boom"))
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=env_text_with_token(), kite=kite
        )
        with client:
            response = client.get("/api/data-sources")
        assert response.status_code == 200


class TestCsrf:
    @pytest.mark.parametrize(
        ("method", "path", "kwargs"),
        [
            ("post", "/api/kite/login", {}),
            ("post", "/api/kite/manual-token", {"json": {"request_token": "x"}}),
            ("post", "/api/kite/disconnect", {}),
            ("post", "/api/settings/check", {}),
            ("put", "/api/settings", {"json": {"values": {"EMA_FAST": 12}}}),
            (
                "post",
                "/api/notifications/telegram/chat-id",
                {},
            ),
            ("post", "/api/practice/check", {}),
        ],
    )
    def test_every_state_changing_route_rejects_a_foreign_origin(
        self, ui, method: str, path: str, kwargs: dict
    ) -> None:
        client, _context, _env, _kite = ui
        response = getattr(client, method)(
            path, headers={"Origin": "https://evil.example"}, **kwargs
        )
        assert response.status_code == 403
        assert "only accepted from the dashboard" in response.json()["detail"]

    def test_same_origin_is_accepted(self, ui) -> None:
        client, _context, _env, _kite = ui
        response = client.post(
            "/api/kite/login", headers={"Origin": BASE_URL, "Sec-Fetch-Site": "same-origin"}
        )
        assert response.status_code == 200


class TestPracticeCheck:
    def test_saved_settings_change_the_replay(self, ui) -> None:
        client, _context, _env, _kite = ui
        first = client.post("/api/practice/check").json()
        assert first["counts"] == {"buys": 1, "sells": 2, "total": 3}
        assert first["label"] == "DEMO"
        assert [alert["side"] for alert in first["alerts"]] == ["SELL", "SELL", "BUY"]
        assert first["alerts"][0]["rules_agreed"] == ["RSI", "MACD", "ORB", "OI"]

        saved = client.put(
            "/api/settings",
            json={"values": {"RULE_EMA_CROSS_REQUIRED": True}},
        )
        assert saved.status_code == 200
        second = client.post("/api/practice/check").json()
        assert second["counts"] == {"buys": 1, "sells": 0, "total": 1}
        assert second["alerts"][0]["side"] == "BUY"
        assert second["alerts"][0]["rules_agreed"] == ["EMA", "RSI", "MACD", "OI"]

    def test_practice_without_settings_is_a_503(self, tmp_path: Path) -> None:
        client, _context, _env, _kite = connect_client(
            tmp_path, env_text=BASE_ENV + "EMA_FAST=oops\n"
        )
        with client:
            response = client.post("/api/practice/check")
        assert response.status_code == 503
        assert response.json()["code"] == "no_settings"

    def test_practice_never_writes_to_the_alert_database(self, ui) -> None:
        client, context, _env, _kite = ui
        before = context.history.read_page().total
        assert client.post("/api/practice/check").status_code == 200
        assert context.history.read_page().total == before


class TestSettingsTriggeredReload:
    def test_saving_a_key_enables_the_connection_without_a_restart(self, tmp_path: Path) -> None:
        client, context, env_path, _kite = connect_client(
            tmp_path,
            env_text="SYMBOLS=NSE:TEST\nKITE_API_SECRET=SECRET-MARKER-9c2b\n",
        )
        with client:
            assert client.post("/api/kite/login").status_code == 400
            saved = client.put(
                "/api/settings",
                json={"values": {"KITE_API_KEY": KEY_MARKER}},
            )
            assert saved.status_code == 200
            assert context.settings is not None
            assert context.settings.kite.api_key == KEY_MARKER
            assert client.post("/api/kite/login").status_code == 200
        assert KEY_MARKER in env_path.read_text(encoding="utf-8")
