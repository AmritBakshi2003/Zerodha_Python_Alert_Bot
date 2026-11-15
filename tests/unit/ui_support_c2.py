"""Shared builders for the C2 (settings + connect) dashboard tests.

Everything stays offline: a fake Kite client, a fake Telegram endpoint and a
temporary ``.env``. No test here can reach Zerodha, Telegram or Slack.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from kite_alerts.ui.app import UiContext, build_context
from unit.ui_support import BASE_URL, FakeMonotonic, make_client

KEY_MARKER = "KEY-MARKER-4f1a"
SECRET_MARKER = "SECRET-MARKER-9c2b"
TOKEN_MARKER = "TOKEN-MARKER-77d0"
REQUEST_MARKER = "REQ-TOKEN-MARKER-3141"
TELEGRAM_MARKER = "TELEGRAM-MARKER-abc"
SLACK_MARKER = "SLACK-MARKER-zzz"
CHAT_MARKER = "CHAT-MARKER-555"
ACCESS_MARKER = "ACCESS-TOKEN-MARKER-8899"

BASE_ENV = (
    "# a comment the writer must preserve\n"
    f"KITE_API_KEY={KEY_MARKER}\n"
    f"KITE_API_SECRET={SECRET_MARKER}\n"
    f"TELEGRAM_BOT_TOKEN=123456:{TELEGRAM_MARKER}\n"
    f"TELEGRAM_CHAT_ID={CHAT_MARKER}\n"
    f"SLACK_WEBHOOK_URL=https://hooks.slack.com/services/{SLACK_MARKER}\n"
    "NOTIFY_CHANNELS=console\n"
    "SYMBOLS=NSE:TEST\n"
)


def write_env(tmp_path: Path, text: str = BASE_ENV, name: str = ".env") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def env_text_with_token(*, token: str = ACCESS_MARKER) -> str:
    return BASE_ENV + f"KITE_ACCESS_TOKEN={token}\n"


class FakeKite:
    """A stand-in for ``KiteConnect`` with every allowed method and no network."""

    def __init__(
        self,
        *,
        profile: dict[str, Any] | None = None,
        profile_error: BaseException | None = None,
        access_token: str = ACCESS_MARKER,
        session_error: BaseException | None = None,
        ltp_result: dict[str, Any] | None = None,
        ltp_error: BaseException | None = None,
        invalidate_error: BaseException | None = None,
    ) -> None:
        self.profile_data = profile if profile is not None else {"user_name": "Demo Trader"}
        self.profile_error = profile_error
        self.access_token = access_token
        self.session_error = session_error
        self.ltp_result = (
            ltp_result if ltp_result is not None else {"NSE:NIFTY 50": {"last_price": 1}}
        )
        self.ltp_error = ltp_error
        self.invalidate_error = invalidate_error
        self.calls: list[str] = []
        self.api_key = ""
        self.token = ""

    def login_url(self) -> str:
        self.calls.append("login_url")
        return f"https://kite.example/connect/login?api_key={self.api_key}&v=3"

    def generate_session(self, request_token: str, api_secret: str) -> dict[str, Any]:
        self.calls.append("generate_session")
        self.last_request_token = request_token
        self.last_secret = api_secret
        if self.session_error is not None:
            raise self.session_error
        return {"access_token": self.access_token}

    def profile(self) -> dict[str, Any]:
        self.calls.append("profile")
        if self.profile_error is not None:
            raise self.profile_error
        return self.profile_data

    def ltp(self, instruments: list[str]) -> dict[str, Any]:
        self.calls.append("ltp")
        if self.ltp_error is not None:
            raise self.ltp_error
        return self.ltp_result

    def invalidate_access_token(self) -> None:
        self.calls.append("invalidate_access_token")
        if self.invalidate_error is not None:
            raise self.invalidate_error


class FakeKiteFactory:
    def __init__(self, kite: FakeKite | None = None) -> None:
        self.kite = kite or FakeKite()
        self.clients: list[tuple[str, str]] = []

    def __call__(self, api_key: str, access_token: str = "") -> FakeKite:
        self.clients.append((api_key, access_token))
        self.kite.api_key = api_key
        self.kite.token = access_token
        return self.kite


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload if payload is not None else {})

    def json(self) -> Any:
        if isinstance(self._payload, BaseException):
            raise ValueError("not json")
        return self._payload


class FakeTelegram:
    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.response = (
            response
            if response is not None
            else FakeResponse(200, {"ok": True, "result": [{"message": {"chat": {"id": 4242}}}]})
        )
        self.error = error
        self.urls: list[str] = []
        self.timeouts: list[float] = []

    def __call__(self, url: str, timeout: float = 10.0) -> Any:
        self.urls.append(url)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.response


def make_c2_context(
    tmp_path: Path,
    *,
    env_text: str = BASE_ENV,
    kite: FakeKite | None = None,
    telegram: FakeTelegram | None = None,
    port: int = 8765,
    fast_saves: bool = True,
) -> tuple[UiContext, Path]:
    env_path = write_env(tmp_path, env_text)
    factory = FakeKiteFactory(kite)
    http = telegram if telegram is not None else FakeTelegram()
    context = build_context(
        env_file=env_path,
        db_path=tmp_path / "alerts.db",
        port=port,
        kite_factory=factory,
        http_get=http,
    )
    assert context.settings_store is not None
    if fast_saves:
        context.settings_store.min_interval = 0.0
    return context, env_path


def make_c2_client(context: UiContext) -> TestClient:
    return make_client(context)


def client_for(
    tmp_path: Path,
    *,
    env_text: str = BASE_ENV,
    kite: FakeKite | None = None,
    telegram: FakeTelegram | None = None,
    port: int = 8765,
    fast_saves: bool = True,
) -> tuple[TestClient, UiContext, Path]:
    context, env_path = make_c2_context(
        tmp_path,
        env_text=env_text,
        kite=kite,
        telegram=telegram,
        port=port,
        fast_saves=fast_saves,
    )
    return make_client(context), context, env_path


def connect_client(
    tmp_path: Path,
    *,
    env_text: str = BASE_ENV,
    kite: FakeKite | None = None,
    port: int = 8765,
    fast_saves: bool = True,
) -> tuple[TestClient, UiContext, Path, FakeKite]:
    """The connect tests want the fake client back as well."""
    fake = kite if kite is not None else FakeKite()
    client, context, env_path = client_for(
        tmp_path,
        env_text=env_text,
        kite=fake,
        port=port,
        fast_saves=fast_saves,
    )
    return client, context, env_path, fake


__all__ = [
    "ACCESS_MARKER",
    "BASE_ENV",
    "BASE_URL",
    "CHAT_MARKER",
    "KEY_MARKER",
    "REQUEST_MARKER",
    "SECRET_MARKER",
    "SLACK_MARKER",
    "TELEGRAM_MARKER",
    "TOKEN_MARKER",
    "FakeKite",
    "FakeKiteFactory",
    "FakeMonotonic",
    "FakeResponse",
    "FakeTelegram",
    "client_for",
    "connect_client",
    "env_text_with_token",
    "make_c2_client",
    "make_c2_context",
    "write_env",
]
