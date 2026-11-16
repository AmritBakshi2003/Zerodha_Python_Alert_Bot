"""Relative paths are anchored to the ``.env`` file's folder (F14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kite_alerts.config.settings import load_settings
from kite_alerts.ui.app import create_app
from unit.ui_support import BASE_URL, make_context


class TestPathAnchoring:
    def test_relative_paths_are_anchored_to_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_dir = tmp_path / "bot"
        env_dir.mkdir()
        env_file = env_dir / ".env"
        env_file.write_text(
            "DB_PATH=data/bot.db\nLOG_DIR=logs\nCACHE_DIR=cache\n",
            encoding="utf-8",
        )
        # Run from an unrelated cwd to prove anchoring is not cwd-dependent.
        monkeypatch.chdir(tmp_path / "elsewhere" if (tmp_path / "elsewhere").exists() else tmp_path)
        other = tmp_path / "elsewhere"
        other.mkdir(exist_ok=True)
        monkeypatch.chdir(other)

        settings = load_settings(env_file)
        assert settings.db_path == (env_dir / "data" / "bot.db").resolve()
        assert settings.observability.log_dir == (env_dir / "logs").resolve()
        assert settings.cache_dir == (env_dir / "cache").resolve()

    def test_about_page_shows_resolved_paths(self, tmp_path: Path) -> None:
        env_dir = tmp_path / "bot"
        env_dir.mkdir()
        env_file = env_dir / ".env"
        env_file.write_text("DB_PATH=data/bot.db\n", encoding="utf-8")

        settings = load_settings(env_file)
        context = make_context(settings, db_path=settings.db_path)
        context.env_file = env_file
        app = create_app(context)
        from fastapi.testclient import TestClient

        with TestClient(app, base_url=BASE_URL) as client:
            payload = client.get("/api/about").json()
            assert "paths" in payload
            assert payload["paths"]["env_file"] == str(env_file)
            assert payload["paths"]["history_db"] is not None
            response = client.get("/api/about.paths")
            assert response.status_code == 200
            assert response.json()["env_file"] == str(env_file)

    def test_dashboard_warns_when_db_differs_from_bot(self, tmp_path: Path) -> None:
        env_dir = tmp_path / "bot"
        env_dir.mkdir()
        env_file = env_dir / ".env"
        env_file.write_text("DB_PATH=data/bot.db\n", encoding="utf-8")

        settings = load_settings(env_file)
        # Dashboard pointed at a different DB than the .env configures.
        context = make_context(settings, db_path=tmp_path / "other.db")
        context.env_file = env_file
        app = create_app(context)
        from fastapi.testclient import TestClient

        with TestClient(app, base_url=BASE_URL) as client:
            payload = client.get("/api/about").json()
            assert payload["paths"]["warnings"], payload
            assert "differ" in payload["paths"]["warnings"][0]
