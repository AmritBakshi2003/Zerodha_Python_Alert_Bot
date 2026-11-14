"""CLI: config-check, version, login token flow, replay and demo."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from conftest import write_env
from kite_alerts.cli.login_cmd import extract_request_token, login, write_access_token
from kite_alerts.cli.main import main
from kite_alerts.core.errors import ConfigError
from kite_alerts.demo import synthetic_day

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


class TestCommands:
    def test_config_check_succeeds(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--env-file", str(env_file(tmp_path)), "config-check"])
        captured = capsys.readouterr()
        assert code == 0
        assert "configuration is valid" in captured.err
        assert '"symbols"' in captured.out

    def test_config_check_reports_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            ["--env-file", str(env_file(tmp_path, EMA_FAST="30", EMA_SLOW="10")), "config-check"]
        )
        assert code == 2
        assert "config error" in capsys.readouterr().err

    def test_missing_symbols_is_valid_but_warned(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--env-file", str(env_file(tmp_path, SYMBOLS="")), "config-check"])
        assert code == 0
        assert "SYMBOLS is empty" in capsys.readouterr().err

    def test_version(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0

    def test_unknown_command_exits(self) -> None:
        with pytest.raises(SystemExit):
            main(["nonsense"])

    def test_demo_runs_offline(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--env-file", str(env_file(tmp_path)), "demo"])
        captured = capsys.readouterr()
        assert code == 0
        assert "BUY DEMO:FUT" in captured.out
        assert "SELL DEMO:FUT" in captured.out


def csv_path(tmp_path: Path) -> Path:
    path = tmp_path / "candles.csv"
    frame = synthetic_day()
    out = frame.reset_index().rename(columns={"index": "time"})
    out["time"] = pd.to_datetime(out["time"]).dt.tz_localize(None)
    out.to_csv(path, index=False)
    return path


class TestReplayCommand:
    def test_replay_csv(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(
            [
                "--env-file",
                str(env_file(tmp_path)),
                "replay",
                "--symbol",
                "DEMO:FUT",
                "--csv",
                str(csv_path(tmp_path)),
            ]
        )
        captured = capsys.readouterr()
        assert code == 0
        assert "ANALYSIS ONLY" in captured.out
        assert "SELL" in captured.out
        assert "BUY" in captured.out

    def test_replay_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(
            [
                "--env-file",
                str(env_file(tmp_path)),
                "replay",
                "--symbol",
                "DEMO:FUT",
                "--csv",
                str(csv_path(tmp_path)),
                "--json",
            ]
        )
        assert code == 0
        assert '"side": "BUY"' in capsys.readouterr().out

    def test_replay_requires_a_source(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--env-file", str(env_file(tmp_path)), "replay", "--symbol", "X"])
        assert code == 2
        assert "--csv" in capsys.readouterr().err


class TestLogin:
    class FakeKiteConnect:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def login_url(self) -> str:
            return "https://kite.trade/connect/login?api_key=key"

        def generate_session(self, request_token: str, api_secret: str) -> dict[str, str]:
            assert api_secret == "secret"
            return {"access_token": f"access-{request_token}"}

    def test_extract_request_token(self) -> None:
        assert extract_request_token("abc123") == "abc123"
        assert (
            extract_request_token("https://127.0.0.1/?request_token=xyz789&action=login")
            == "xyz789"
        )

    def test_write_creates_and_preserves_keys(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("# comment\nKITE_API_KEY=key\nKITE_ACCESS_TOKEN=old\n", encoding="utf-8")
        write_access_token(path, "new-token")
        text = path.read_text(encoding="utf-8")
        assert "# comment" in text
        assert "KITE_API_KEY=key" in text
        assert "KITE_ACCESS_TOKEN=new-token" in text
        assert "old" not in text

    def test_write_creates_missing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / ".env"
        write_access_token(path, "token")
        assert "KITE_ACCESS_TOKEN=token" in path.read_text(encoding="utf-8")
        if os.name == "posix":
            assert (path.stat().st_mode & 0o777) == 0o600

    def test_login_saves_the_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiteconnect

        monkeypatch.setattr(kiteconnect, "KiteConnect", self.FakeKiteConnect)
        path = env_file(tmp_path)
        code = login(env_file=path, request_token="req123")
        assert code == 0
        assert "KITE_ACCESS_TOKEN=access-req123" in path.read_text(encoding="utf-8")

    def test_login_accepts_a_redirect_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiteconnect

        monkeypatch.setattr(kiteconnect, "KiteConnect", self.FakeKiteConnect)
        path = env_file(tmp_path)
        login(env_file=path, request_token="https://x/?request_token=url999&action=login")
        assert "access-url999" in path.read_text(encoding="utf-8")

    def test_login_prints_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import kiteconnect

        monkeypatch.setattr(kiteconnect, "KiteConnect", self.FakeKiteConnect)
        path = env_file(tmp_path)
        before = path.read_text(encoding="utf-8")
        assert login(env_file=path, request_token="abc", print_only=True) == 0
        assert "access-abc" in capsys.readouterr().out
        assert path.read_text(encoding="utf-8") == before

    def test_login_requires_credentials(self, tmp_path: Path) -> None:
        path = write_env(tmp_path / ".env", {"SYMBOLS": "NSE:TEST"})
        with pytest.raises(ConfigError, match="KITE_API_KEY"):
            login(env_file=path, request_token="abc")
