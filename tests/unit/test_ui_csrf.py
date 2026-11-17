"""CSRF protection for the dashboard's state-changing routes (F6)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kite_alerts.ui.app import create_app
from kite_alerts.ui.security import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
)
from unit.ui_support import BASE_URL, make_context, make_settings


def _raw_client(tmp_path: Path) -> TestClient:
    settings = make_settings(tmp_path / "alerts.db")
    context = make_context(settings)
    return TestClient(create_app(context), base_url=BASE_URL)


class TestCsrf:
    def test_reads_do_not_require_the_token(self, tmp_path: Path) -> None:
        client = _raw_client(tmp_path)
        with client:
            assert client.get("/api/status").status_code == 200
            assert client.get("/api/about").status_code == 200
            assert client.get("/").status_code == 200

    def test_mutation_without_csrf_token_is_403(self, tmp_path: Path) -> None:
        client = _raw_client(tmp_path)
        with client:
            response = client.post(
                "/api/test-alert",
                headers={"Origin": BASE_URL, "Sec-Fetch-Site": "same-origin"},
            )
            assert response.status_code == 403
            assert "token" in response.json()["detail"].lower()

    def test_mutation_with_token_but_foreign_origin_is_403(self, tmp_path: Path) -> None:
        client = _raw_client(tmp_path)
        with client:
            # Same cookie/header pair, but the request comes from a foreign origin.
            response = client.post(
                "/api/test-alert",
                headers={
                    "Origin": "https://evil.example",
                    "Cookie": f"{CSRF_COOKIE_NAME}=abc123",
                    CSRF_HEADER_NAME: "abc123",
                },
            )
            assert response.status_code == 403

    def test_mutation_with_mismatched_token_is_403(self, tmp_path: Path) -> None:
        client = _raw_client(tmp_path)
        with client:
            # Seed a cookie via GET /, then send a wrong header value.
            first = client.get("/")
            cookie = first.cookies.get(CSRF_COOKIE_NAME)
            assert cookie
            response = client.post(
                "/api/test-alert",
                headers={
                    "Origin": BASE_URL,
                    "Sec-Fetch-Site": "same-origin",
                    "Cookie": f"{CSRF_COOKIE_NAME}={cookie}",
                    CSRF_HEADER_NAME: "not-the-token",
                },
            )
            assert response.status_code == 403

    def test_csrf_token_is_stable_within_a_session(self, tmp_path: Path) -> None:
        client = _raw_client(tmp_path)
        with client:
            first = client.get("/")
            second = client.get("/")
            cookie_one = client.cookies.get(CSRF_COOKIE_NAME)
            assert cookie_one
            assert f'content="{cookie_one}"' in first.text
            assert f'content="{cookie_one}"' in second.text

    def test_about_exposes_the_token_for_curl(self, tmp_path: Path) -> None:
        client = _raw_client(tmp_path)
        with client:
            response = client.get("/api/about")
            assert response.status_code == 200
            token = response.json()["csrf_token"]
            assert token
            assert token == response.cookies.get(CSRF_COOKIE_NAME)

    def test_mutation_with_valid_token_succeeds(self, tmp_path: Path) -> None:
        client = _raw_client(tmp_path)
        with client:
            about = client.get("/api/about")
            token = about.json()["csrf_token"]
            cookie = about.cookies.get(CSRF_COOKIE_NAME)
            response = client.post(
                "/api/test-alert",
                headers={
                    "Origin": BASE_URL,
                    "Sec-Fetch-Site": "same-origin",
                    "Cookie": f"{CSRF_COOKIE_NAME}={cookie}",
                    CSRF_HEADER_NAME: token,
                },
            )
            assert response.status_code == 200

    def test_static_api_helper_sends_the_header(self) -> None:
        from unit.ui_support import REPO_ROOT

        source = (REPO_ROOT / "src" / "kite_alerts" / "ui" / "static" / "dom.js").read_text(
            encoding="utf-8"
        )
        assert "X-Kite-Alerts-CSRF" in source
        assert 'meta[name="kite-alerts-csrf"]' in source

    def test_curl_handshake_is_documented(self) -> None:
        """F6 acceptance: the non-browser path has to be written down."""
        from unit.ui_support import REPO_ROOT

        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "/api/about" in readme
        assert "X-Kite-Alerts-CSRF" in readme
