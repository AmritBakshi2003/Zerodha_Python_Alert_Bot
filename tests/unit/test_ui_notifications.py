"""Guided Telegram setup: chat-id detection, offline and always redacted."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from unit.ui_support_c2 import (
    BASE_ENV,
    TELEGRAM_MARKER,
    FakeResponse,
    FakeTelegram,
    client_for,
)


@pytest.fixture
def ui(tmp_path: Path):
    telegram = FakeTelegram()
    client, context, env_path = client_for(tmp_path, telegram=telegram)
    with client:
        yield client, context, env_path, telegram


def post_chat_id(client) -> dict:
    response = client.post("/api/notifications/telegram/chat-id")
    assert response.status_code == 200
    return response.json()


class TestChatId:
    def test_finds_the_newest_chat_id(self, ui) -> None:
        client, _context, _env, telegram = ui
        body = post_chat_id(client)
        assert body["ok"] is True
        assert body["chat_id"] == "4242"
        assert body["code"] == "found"
        assert len(telegram.urls) == 1
        assert telegram.urls[0].startswith("https://api.telegram.org/bot")
        assert telegram.urls[0].endswith("/getUpdates")
        assert telegram.timeouts == [10.0]

    def test_the_response_never_carries_the_bot_token(self, ui) -> None:
        client, _context, _env, _telegram = ui
        response = client.post("/api/notifications/telegram/chat-id")
        assert TELEGRAM_MARKER not in response.text

    def test_the_last_message_wins(self, tmp_path: Path) -> None:
        payload = {
            "ok": True,
            "result": [
                {"message": {"chat": {"id": 11}}},
                {"edited_message": {"chat": {"id": 22}}},
                {"channel_post": {"chat": {"id": 33}}},
            ],
        }
        client, _context, _env = client_for(
            tmp_path, telegram=FakeTelegram(FakeResponse(200, payload))
        )
        with client:
            assert post_chat_id(client)["chat_id"] == "33"

    def test_without_a_token_it_explains_what_to_do(self, tmp_path: Path) -> None:
        telegram = FakeTelegram()
        client, _context, _env = client_for(
            tmp_path,
            env_text="SYMBOLS=NSE:TEST\nTELEGRAM_CHAT_ID=1\n",
            telegram=telegram,
        )
        with client:
            body = post_chat_id(client)
        assert body["ok"] is False
        assert body["code"] == "no_token"
        assert "Save your Telegram bot token" in body["message"]
        assert telegram.urls == []

    def test_no_recent_messages_is_explained(self, tmp_path: Path) -> None:
        telegram = FakeTelegram(FakeResponse(200, {"ok": True, "result": []}))
        client, _context, _env = client_for(tmp_path, telegram=telegram)
        with client:
            body = post_chat_id(client)
        assert body["code"] == "no_updates"
        assert "press Start" in body["message"]

    @pytest.mark.parametrize(
        ("status", "payload", "code"),
        [
            (401, {"ok": False}, "token_rejected"),
            (409, {"ok": False}, "webhook"),
            (429, {"ok": False}, "rate_limited"),
            (500, {"ok": False}, "telegram_error"),
            (200, {"ok": False}, "telegram_error"),
        ],
    )
    def test_telegram_errors_map_to_plain_codes(
        self, tmp_path: Path, status: int, payload: dict, code: str
    ) -> None:
        telegram = FakeTelegram(FakeResponse(status, payload))
        client, _context, _env = client_for(tmp_path, telegram=telegram)
        with client:
            body = post_chat_id(client)
        assert body["ok"] is False
        assert body["code"] == code
        assert body["message"]

    def test_a_network_failure_is_a_plain_message(self, tmp_path: Path) -> None:
        telegram = FakeTelegram(error=requests.ConnectionError("offline"))
        client, _context, _env = client_for(tmp_path, telegram=telegram)
        with client:
            body = post_chat_id(client)
        assert body["code"] == "unreachable"
        assert "internet connection" in body["message"]

    def test_an_unexpected_error_is_still_unreachable(self, tmp_path: Path) -> None:
        telegram = FakeTelegram(error=RuntimeError("odd"))
        client, _context, _env = client_for(tmp_path, telegram=telegram)
        with client:
            body = post_chat_id(client)
        assert body["code"] == "unreachable"

    def test_non_json_200_is_no_updates(self, tmp_path: Path) -> None:
        telegram = FakeTelegram(FakeResponse(200, ValueError("not json"), text="<html>"))
        client, _context, _env = client_for(tmp_path, telegram=telegram)
        with client:
            body = post_chat_id(client)
        assert body["code"] == "no_updates"

    def test_broken_settings_are_reported(self, tmp_path: Path) -> None:
        telegram = FakeTelegram()
        client, _context, _env = client_for(
            tmp_path, env_text=BASE_ENV + "EMA_FAST=oops\n", telegram=telegram
        )
        with client:
            body = post_chat_id(client)
        assert body["code"] == "settings"
        assert telegram.urls == []


class TestChannels:
    def test_channels_are_saved_and_reported(self, ui) -> None:
        client, _context, env_path, _telegram = ui
        response = client.put(
            "/api/settings",
            json={"values": {"NOTIFY_CHANNELS": ["telegram", "slack"]}},
        )
        assert response.status_code == 200
        assert "NOTIFY_CHANNELS=telegram,slack" in env_path.read_text(encoding="utf-8")
        payload = client.get("/api/settings").json()
        field = next(
            item
            for group in payload["groups"]
            for item in group["fields"]
            if item["key"] == "NOTIFY_CHANNELS"
        )
        assert field["value"] == ["telegram", "slack"]
        assert field["choices"] == ["console", "telegram", "slack", "smtp"]

    def test_console_is_always_available(self, ui) -> None:
        client, _context, _env, _telegram = ui
        payload = client.get("/api/settings").json()
        field = next(
            item
            for group in payload["groups"]
            for item in group["fields"]
            if item["key"] == "NOTIFY_CHANNELS"
        )
        assert "console" in field["choices"]
