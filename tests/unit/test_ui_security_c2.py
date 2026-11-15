"""C2 security: no credential ever leaves the process, secrets are gitignored."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import kite_alerts
from kite_alerts.ui.app import create_app
from kite_alerts.ui.kite_connect import STATE_COOKIE
from unit.ui_support_c2 import (
    ACCESS_MARKER,
    CHAT_MARKER,
    KEY_MARKER,
    REQUEST_MARKER,
    SECRET_MARKER,
    SLACK_MARKER,
    TELEGRAM_MARKER,
    TOKEN_MARKER,
    connect_client,
    env_text_with_token,
)

MARKERS = (
    KEY_MARKER,
    SECRET_MARKER,
    TOKEN_MARKER,
    ACCESS_MARKER,
    REQUEST_MARKER,
    TELEGRAM_MARKER,
    SLACK_MARKER,
)

#: Every C2 endpoint, including failure paths. Each runs against a fresh app.
CASES: list[tuple[str, str, dict | None]] = [
    ("get", "/api/settings", None),
    ("post", "/api/settings/check", None),
    ("put", "/api/settings", {"values": {"EMA_FAST": 5000}}),
    ("put", "/api/settings", {"values": {"EVIL_KEY": "x"}}),
    ("put", "/api/settings", {"values": {"KITE_API_KEY": "SHOULD-NOT-BE-ECHOED"}}),
    ("get", "/api/kite/session", None),
    ("get", "/api/data-sources", None),
    ("get", "/api/data-sources?refresh=1", None),
    ("post", "/api/kite/login", None),
    ("post", "/api/kite/manual-token", {"request_token": REQUEST_MARKER}),
    ("post", "/api/kite/disconnect", None),
    ("post", "/api/notifications/telegram/chat-id", None),
    ("post", "/api/practice/check", None),
    ("get", "/api/about", None),
    ("get", "/api/status", None),
    ("get", "/api/chart/feed-status", None),
    ("post", "/api/kite/simulate/disconnect", None),
    ("post", "/api/kite/simulate/token-expiry", None),
    ("post", "/api/kite/simulate/reconnect", None),
]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    CASES,
    ids=[f"{method}-{path.replace('/', '_')}" for method, path, _ in CASES],
)
def test_no_endpoint_returns_a_credential(
    tmp_path: Path, method: str, path: str, body: dict | None
) -> None:
    client, _context, _env, _kite = connect_client(
        tmp_path, env_text=env_text_with_token(token=ACCESS_MARKER)
    )
    with client:
        response = (
            getattr(client, method)(path, json=body)
            if body is not None
            else getattr(client, method)(path)
        )
        text = response.text
    for marker in MARKERS:
        assert marker not in text, f"{marker} leaked from {method.upper()} {path}"
    assert "SHOULD-NOT-BE-ECHOED" not in text


def test_the_start_redirect_keeps_the_key_out_of_the_json(tmp_path: Path) -> None:
    client, _context, _env, _kite = connect_client(tmp_path)
    with client:
        login = client.post("/api/kite/login")
        assert KEY_MARKER not in login.text
        assert client.cookies.get(STATE_COOKIE)
        start = client.get("/kite/start", follow_redirects=False)
    assert start.status_code == 302
    # Kite's own login URL must carry the API key; it is a browser redirect, not
    # a response any script here can read.
    assert KEY_MARKER in start.headers["location"]
    assert KEY_MARKER not in start.text


def test_error_bodies_do_not_echo_the_rejected_token(tmp_path: Path) -> None:
    client, _context, _env, _kite = connect_client(tmp_path, env_text=env_text_with_token())
    with client:
        response = client.post(
            "/api/notifications/telegram/chat-id",
            json={},
        )
        assert TELEGRAM_MARKER not in response.text
        bad = client.post("/api/kite/manual-token", json={"request_token": REQUEST_MARKER})
        assert REQUEST_MARKER not in bad.text
        assert "wrong" in bad.json()["message"] or "timed out" in bad.json()["message"]


def test_settings_never_expose_a_secret_even_when_set(tmp_path: Path) -> None:
    client, _context, _env, _kite = connect_client(
        tmp_path, env_text=env_text_with_token(token=TOKEN_MARKER)
    )
    with client:
        payload = client.get("/api/settings").json()
    secrets = [field for group in payload["groups"] for field in group["fields"] if field["secret"]]
    assert secrets
    for field in secrets:
        assert "value" not in field
        assert "is_set" in field
    assert CHAT_MARKER not in str(secrets)


def test_csp_and_headers_still_apply_to_new_routes(tmp_path: Path) -> None:
    client, _context, _env, _kite = connect_client(tmp_path)
    paths = (
        "/api/settings",
        "/api/kite/session",
        "/api/data-sources",
        "/api/chart/feed-status",
        "/",
    )
    with client:
        for path in paths:
            response = client.get(path)
            assert response.headers["content-security-policy"]
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["cache-control"] == "no-store"
            assert "access-control-allow-origin" not in response.headers


def test_a_foreign_host_cannot_read_the_new_routes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    _client, context, _env, _kite = connect_client(tmp_path)
    paths = (
        "/api/settings",
        "/api/kite/session",
        "/api/data-sources",
        "/api/chart/feed-status",
    )
    with TestClient(create_app(context), base_url="http://evil.example:8765") as evil:
        for path in paths:
            response = evil.get(path)
            assert response.status_code == 400
            assert "unexpected host name" in response.json()["detail"]


class TestGitignore:
    def repo_root(self) -> Path:
        return Path(kite_alerts.__file__).resolve().parents[2]

    def test_env_files_and_backups_are_ignored(self) -> None:
        root = self.repo_root()
        text = (root / ".gitignore").read_text(encoding="utf-8")
        lines = {line.strip() for line in text.splitlines()}
        assert ".env" in lines
        assert ".env.*" in lines, "timestamped .env backups and temp files must be ignored"
        assert "!.env.example" in lines, "the template must stay committable"

    @pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
    def test_git_itself_ignores_them(self) -> None:
        root = self.repo_root()
        if not (root / ".git").exists():
            pytest.skip("not a git checkout")
        for name in (".env", ".env.bak-20260101-120000"):
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", "-q", name],
                cwd=root,
                check=False,
                capture_output=True,
            )
            assert result.returncode == 0, f"{name} is not git-ignored"
        assert (
            subprocess.run(
                ["git", "check-ignore", "--no-index", "-q", ".env.example"],
                cwd=root,
                check=False,
                capture_output=True,
            ).returncode
            != 0
        )


def test_env_template_has_placeholders_only(tmp_path: Path) -> None:
    root = Path(kite_alerts.__file__).resolve().parents[2]
    example = root / ".env.example"
    if not example.exists():
        pytest.skip("no .env.example in this checkout")
    text = example.read_text(encoding="utf-8")
    for marker in MARKERS:
        assert marker not in text
    assert "your_" in text.lower() or "xxxx" in text.lower() or "=" in text


def test_ui_source_does_not_log_credentials() -> None:
    root = Path(kite_alerts.__file__).parent / "ui"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower()
            if "log." not in lowered:
                continue
            if any(
                word in lowered
                for word in (
                    "get_secret_value",
                    "secret_value",
                    "webhook_url",
                    "bot_token",
                    "request_token",
                    "access_token",
                    "api_secret",
                )
            ):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, f"possible credential logging: {offenders}"
