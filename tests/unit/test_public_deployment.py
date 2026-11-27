"""A deliberate public deployment must serve the dashboard without weakening the guard.

The dashboard is loopback-only by default: ``SecurityMiddleware`` refuses a
foreign ``Host`` header so DNS rebinding cannot reach a dashboard running on
someone's own machine. A hosted deployment (Vercel) is a different situation —
there is no local machine to protect — so the middleware accepts the host names
the hosting platform says it is serving the build from, plus anything the owner
lists explicitly in ``KITE_ALERTS_ALLOWED_HOSTS``.

These tests pin both halves: the public host serves the real dashboard when it is
allowed, and is still refused when it is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kite_alerts.ui import app as app_module
from kite_alerts.ui.app import create_app
from kite_alerts.ui.security import (
    ALLOWED_HOSTS_ENV,
    allowed_host_names,
    host_is_allowed,
)
from unit.ui_support import make_context, make_settings

DEPLOY_HOST = "zerodha-python-alert-bot.vercel.app"
PREVIEW_HOST = "zerodha-python-alert-bot-abc123.vercel.app"

_HOST_ENV_VARS = (
    ALLOWED_HOSTS_ENV,
    "VERCEL",
    "VERCEL_ENV",
    "VERCEL_URL",
    "VERCEL_PROJECT_PRODUCTION_URL",
    "VERCEL_BRANCH_URL",
)


@pytest.fixture(autouse=True)
def _clean_host_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts as a local install with no hosting variables set."""
    for name in _HOST_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _client(tmp_path: Path, *, base_url: str) -> TestClient:
    settings = make_settings(tmp_path / "alerts.db")
    context = make_context(settings, db_path=settings.db_path)
    return TestClient(create_app(context), base_url=base_url)


def test_public_host_is_refused_without_an_explicit_opt_in(tmp_path: Path) -> None:
    client = _client(tmp_path, base_url=f"https://{DEPLOY_HOST}")
    response = client.get("/")
    assert response.status_code == 400
    assert "unexpected host name" in response.json()["detail"].lower()


def test_allow_listed_host_serves_the_real_dashboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, DEPLOY_HOST)
    client = _client(tmp_path, base_url=f"https://{DEPLOY_HOST}")

    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<!doctype html" in page.text.lower()
    # The page the local run serves, not the "assets not bundled" fallback.
    assert "kite-alerts" in page.text.lower()
    assert "not present in this deployment" not in page.text

    styles = client.get("/static/styles.css")
    assert styles.status_code == 200
    assert "text/css" in styles.headers["content-type"]


def test_vercel_production_host_is_accepted_automatically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", DEPLOY_HOST)
    client = _client(tmp_path, base_url=f"https://{DEPLOY_HOST}")

    response = client.get("/api/about")
    assert response.status_code == 200
    assert response.json()["version"]


def test_vercel_preview_host_is_accepted_automatically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_URL", PREVIEW_HOST)
    client = _client(tmp_path, base_url=f"https://{PREVIEW_HOST}")

    assert client.get("/api/status").status_code == 200


def test_another_vercel_app_host_is_refused_on_a_local_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "dashboard.example.com")
    client = _client(tmp_path, base_url=f"https://{PREVIEW_HOST}")
    assert client.get("/").status_code == 400


def test_loopback_stays_allowed_in_a_hosted_deployment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VERCEL", "1")
    client = _client(tmp_path, base_url="http://127.0.0.1:8765")
    assert client.get("/").status_code == 200


def test_custom_domain_is_matched_without_its_port() -> None:
    names = allowed_host_names({ALLOWED_HOSTS_ENV: " Kite.Example.COM:443 , "})
    assert names == frozenset({"kite.example.com"})
    assert host_is_allowed("kite.example.com", names)
    assert host_is_allowed("kite.example.com:443", names)
    assert not host_is_allowed("evil.example.com", names)


def test_vercel_suffix_is_trusted_only_inside_a_hosted_deployment() -> None:
    assert not host_is_allowed(PREVIEW_HOST, allowed_host_names({}))
    hosted = allowed_host_names({"VERCEL": "1"})
    assert host_is_allowed(PREVIEW_HOST, hosted)
    assert not host_is_allowed("evil.example.com", hosted)


def test_loopback_is_accepted_without_any_configuration() -> None:
    assert host_is_allowed("localhost:8765", allowed_host_names({}))
    assert host_is_allowed("127.0.0.1:8765", allowed_host_names({}))
    assert not host_is_allowed("example.com", allowed_host_names({}))


def test_practice_replay_works_on_the_public_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The demo is the point of the deployment, so prove it answers end to end."""
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", DEPLOY_HOST)
    client = _client(tmp_path, base_url=f"https://{DEPLOY_HOST}")

    page = client.get("/")
    assert page.status_code == 200
    token = client.cookies.get("kite_alerts_csrf") or ""
    assert token
    response = client.post(
        "/api/practice/check",
        json={},
        headers={"X-Kite-Alerts-CSRF": token, "Origin": f"https://{DEPLOY_HOST}"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["demo"] is True
    assert payload["counts"]["total"] >= 0


def test_missing_vendored_file_returns_404_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bundle without the vendored licence must answer honestly, not crash."""
    monkeypatch.setattr(app_module, "LICENSE_FILE", tmp_path / "absent-LICENSE")
    client = _client(tmp_path, base_url="http://127.0.0.1:8765")
    response = client.get("/license")
    assert response.status_code == 404
    assert "not bundled" in response.json()["detail"].lower()


def test_vercel_json_ships_the_dashboard_assets() -> None:
    """Vercel only bundles traced Python imports; the assets need includeFiles."""
    config = json.loads((Path(__file__).resolve().parents[2] / "vercel.json").read_text("utf-8"))
    entry = config["functions"]["asgi.py"]
    includes = entry["includeFiles"]
    pattern = includes[1:-1] if isinstance(includes, str) and includes.startswith("{") else includes
    assert "src/kite_alerts/ui/static/**" in pattern
    assert "config/**" in pattern
    assert (Path(__file__).resolve().parents[2] / "src/kite_alerts/ui/static/index.html").is_file()
    assert (Path(__file__).resolve().parents[2] / "config/holidays.yaml").is_file()
