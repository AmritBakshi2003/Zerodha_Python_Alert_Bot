"""Guided notification setup: finding a Telegram chat id.

The only network call in C2 that is not Kite. It reads the bot's own recent
updates so the user does not have to understand ``getUpdates`` JSON, and it never
echats the bot token or a full webhook URL back to the browser.

Tests inject ``http_get``; nothing here touches the network under test.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from ..config.settings import Settings
from ..notifiers.base import redact

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TIMEOUT_SECONDS = 10.0

NO_TOKEN = "Save your Telegram bot token first (the field above), then press “Find my chat id”."
NO_UPDATES = (
    "Telegram has no recent messages for this bot. Open your bot in Telegram, press "
    "Start, then try again."
)
TOKEN_REJECTED = "Telegram rejected that bot token. Copy it again from @BotFather."
WEBHOOK_SET = (
    "This bot has a webhook configured, so Telegram will not return updates here. "
    "Remove the webhook or enter the chat id manually."
)
UNREACHABLE = "Could not reach Telegram. Check your internet connection and try again."


@dataclass(frozen=True, slots=True)
class ChatIdResult:
    ok: bool
    chat_id: str = ""
    message: str = ""
    code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "chat_id": self.chat_id,
            "message": self.message,
            "code": self.code,
        }


def _chat_id_from_updates(payload: Any) -> str:
    """Newest chat id in a getUpdates response, or an empty string."""
    if not isinstance(payload, dict):
        return ""
    updates = payload.get("result")
    if not isinstance(updates, list):
        return ""
    for update in reversed(updates):
        if not isinstance(update, dict):
            continue
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            item = update.get(key)
            if isinstance(item, dict):
                chat = item.get("chat")
                if isinstance(chat, dict) and chat.get("id") is not None:
                    return str(chat["id"])
    return ""


@dataclass
class TelegramSetupService:
    """Reads the user's own bot updates to fill in ``TELEGRAM_CHAT_ID``."""

    settings_provider: Callable[[], Settings | None]
    http_get: Callable[..., Any] = requests.get

    def find_chat_id(self) -> ChatIdResult:
        settings = self.settings_provider()
        if settings is None:
            return ChatIdResult(
                ok=False,
                code="settings",
                message="Your settings could not be read, so the bot token is unavailable.",
            )
        token = settings.notify.telegram_bot_token.get_secret_value()
        if not token:
            return ChatIdResult(ok=False, code="no_token", message=NO_TOKEN)

        url = f"{TELEGRAM_API}/bot{token}/getUpdates"
        try:
            response = self.http_get(url, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            log.warning("telegram getUpdates failed: %s", redact(str(exc))[:120])
            return ChatIdResult(ok=False, code="unreachable", message=UNREACHABLE)
        except Exception:
            log.warning("telegram getUpdates failed")
            return ChatIdResult(ok=False, code="unreachable", message=UNREACHABLE)

        status = int(getattr(response, "status_code", 0) or 0)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if status == 401:
            return ChatIdResult(ok=False, code="token_rejected", message=TOKEN_REJECTED)
        if status == 409:
            return ChatIdResult(ok=False, code="webhook", message=WEBHOOK_SET)
        if status == 429:
            return ChatIdResult(
                ok=False,
                code="rate_limited",
                message="Telegram is rate limiting this bot. Wait a minute, then try again.",
            )
        if status >= 400:
            return ChatIdResult(
                ok=False,
                code="telegram_error",
                message="Telegram could not answer that request. Check the bot token.",
            )
        if isinstance(payload, dict) and payload.get("ok") is False:
            return ChatIdResult(
                ok=False,
                code="telegram_error",
                message="Telegram rejected that request. Check the bot token.",
            )

        chat_id = _chat_id_from_updates(payload)
        if not chat_id:
            return ChatIdResult(ok=False, code="no_updates", message=NO_UPDATES)
        return ChatIdResult(
            ok=True,
            chat_id=chat_id,
            code="found",
            message=f"Found chat id {chat_id}. Save the settings to use it.",
        )


__all__ = [
    "TELEGRAM_API",
    "ChatIdResult",
    "TelegramSetupService",
]
