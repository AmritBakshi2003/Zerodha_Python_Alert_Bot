"""The Vercel entrypoint must expose the real app without weakening its guards.

Vercel's Python builder needs a module-level ``app`` object in a root-level file.
``asgi.py`` supplies it. These tests load that exact file (not a copy of the
wiring) and prove the two properties that matter: the object is a FastAPI app,
and the loopback-only DNS-rebinding guard is still active on it.
"""

from __future__ import annotations

import runpy
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


def _entrypoint() -> FastAPI:
    namespace = runpy.run_path(str(ROOT / "asgi.py"))
    app = namespace["app"]
    assert isinstance(app, FastAPI)
    return app


def test_vercel_entrypoint_exports_a_fastapi_app() -> None:
    """Vercel's entrypoint discovery finds a real FastAPI instance."""
    assert isinstance(_entrypoint(), FastAPI)


def test_entrypoint_keeps_the_loopback_only_guard() -> None:
    """A public hostname is still refused even through the deployment entrypoint."""
    client = TestClient(_entrypoint(), base_url="http://example.com")
    response = client.get("/")
    assert response.status_code == 400
    assert "unexpected host name" in response.json()["detail"].lower()


def test_entrypoint_serves_the_dashboard_on_loopback() -> None:
    """On loopback the same app serves the dashboard page."""
    client = TestClient(_entrypoint(), base_url="http://127.0.0.1:8765")
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
