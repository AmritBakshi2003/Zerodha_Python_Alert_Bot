"""F11 — ``main()`` exit codes and subcommand wiring (the product entry point)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from conftest import write_env
from kite_alerts.core.errors import AuthError, ConfigError, KiteAlertsError

cli_main = importlib.import_module("kite_alerts.cli.main")

BASE_ENV = {
    "KITE_API_KEY": "key",
    "KITE_API_SECRET": "secret",
    "KITE_ACCESS_TOKEN": "token",
    "SYMBOLS": "NSE:TEST",
}


def env_file(tmp_path: Path, **overrides: str) -> Path:
    values = dict(BASE_ENV)
    values.update(overrides)
    return write_env(tmp_path / ".env", values)


class _Hub:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_text(self, text: str, **_kwargs: object) -> None:
        self.texts.append(text)


class _Runtime:
    def __init__(self) -> None:
        self.hub = _Hub()


class TestRunCommand:
    def test_prints_the_alerts_only_banner(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        runtime = _Runtime()
        monkeypatch.setattr(cli_main, "build_runtime", lambda _settings: runtime)
        monkeypatch.setattr(
            cli_main,
            "run_live",
            lambda _settings, runtime=None, duration_seconds=None: 0,
        )

        code = main_with(tmp_path, "run")

        captured = capsys.readouterr()
        assert code == 0
        assert "ALERTS ONLY, no orders" in captured.err
        assert "Watching NSE:TEST" in captured.err
        assert "Ctrl-C to stop" in captured.err

    def test_config_warnings_are_printed_and_sent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        runtime = _Runtime()
        monkeypatch.setattr(cli_main, "build_runtime", lambda _settings: runtime)
        monkeypatch.setattr(
            cli_main,
            "run_live",
            lambda _settings, runtime=None, duration_seconds=None: 0,
        )

        code = main_with(tmp_path, "run", env={"SYMBOLS": ""})

        assert code == 0
        assert "warning: SYMBOLS is empty" in capsys.readouterr().err
        assert any("⚠️ config: SYMBOLS is empty" in text for text in runtime.hub.texts)

    def test_auth_error_maps_to_exit_3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fail(_settings: object) -> None:
            raise AuthError("token expired")

        monkeypatch.setattr(cli_main, "build_runtime", fail)
        code = main_with(tmp_path, "run")
        assert code == 3
        assert "auth error: token expired" in capsys.readouterr().err

    def test_config_error_maps_to_exit_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fail(_settings: object) -> None:
            raise ConfigError("bad config")

        monkeypatch.setattr(cli_main, "build_runtime", fail)
        code = main_with(tmp_path, "run")
        assert code == 2
        assert "config error: bad config" in capsys.readouterr().err

    def test_a_kite_error_maps_to_exit_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fail(_settings: object) -> None:
            raise KiteAlertsError("broken")

        monkeypatch.setattr(cli_main, "build_runtime", fail)
        code = main_with(tmp_path, "run")
        assert code == 1
        assert "error: broken" in capsys.readouterr().err

    def test_ctrl_c_is_a_clean_exit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def interrupted(_settings: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_main, "build_runtime", interrupted)
        assert main_with(tmp_path, "run") == 0


class TestOtherCommands:
    def test_login_delegates_and_returns_its_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kite_alerts.cli import login_cmd

        calls: list[Path] = []

        def fake_login(*, env_file: Path) -> int:
            calls.append(env_file)
            return 0

        monkeypatch.setattr(login_cmd, "login", fake_login)
        path = env_file(tmp_path)
        assert cli_main.main(["--env-file", str(path), "login"]) == 0
        assert [Path(item) for item in calls] == [path]

    def test_ui_delegates_to_the_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kite_alerts.ui import server

        captured: dict[str, object] = {}

        def fake_run_ui(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(server, "run_ui", fake_run_ui)
        code = cli_main.main(
            [
                "--env-file",
                str(env_file(tmp_path)),
                "ui",
                "--host",
                "127.0.0.1",
                "--port",
                "9999",
                "--simulate-kite",
            ]
        )
        assert code == 0
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 9999
        assert captured["simulate_kite"] is True

    def test_ui_without_the_optional_packages_explains_how_to_install(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setitem(sys.modules, "kite_alerts.ui.server", None)

        code = cli_main.main(["--env-file", str(env_file(tmp_path)), "ui"])

        captured = capsys.readouterr()
        assert code == 2
        assert "pip install -e" in captured.err
        assert ".[ui]" in captured.err


def main_with(tmp_path: Path, command: str, *, env: dict[str, str] | None = None) -> int:
    overrides = dict(env or {})
    return cli_main.main(["--env-file", str(env_file(tmp_path, **overrides)), command])
