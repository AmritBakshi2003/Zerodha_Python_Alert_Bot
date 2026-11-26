"""The Vercel entrypoint must expose the real app without weakening its guards.

Vercel's Python builder needs a module-level ``app`` object in a root-level file.
``asgi.py`` supplies it. These tests load that exact file (not a copy of the
wiring) and prove the two properties that matter: the object is a FastAPI app,
and the loopback-only DNS-rebinding guard is still active on it.
"""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
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


def test_entrypoint_bootstraps_src_layout_for_vercel() -> None:
    """The entrypoint works when only the repo root is on sys.path (Vercel)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "import asgi; print(type(asgi.app).__name__)"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "FastAPI" in result.stdout


def test_flat_requirements_include_the_asgi_runtime() -> None:
    """Vercel installs requirements.txt; fastapi must be listed there.

    Vercel's runtime log showed `ModuleNotFoundError: fastapi` because the flat
    requirements file mirrored only [project.dependencies], not the `ui` extra
    that the asgi entrypoint imports.
    """
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    packages = {
        line.strip().split(">=")[0].split("<")[0].lower()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {"fastapi", "uvicorn", "httpx", "yfinance"} <= packages
