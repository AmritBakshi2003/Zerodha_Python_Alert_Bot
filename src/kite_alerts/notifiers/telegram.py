"""Telegram Bot API notifier."""

from __future__ import annotations

from typing import Any

import requests

from ..core.clock import IST
from ..core.models import Message
from .base import BaseNotifier, DeliveryError

TELEGRAM_LIMIT = 4096


class TelegramNotifier(BaseNotifier):
    name = "telegram"
    max_length = TELEGRAM_LIMIT

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        timeout: float = 10.0,
        session: requests.Session | None = None,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        super().__init__(timeout=timeout, session=session)
        if not bot_token or ":" not in bot_token:
            raise ValueError("telegram bot token looks malformed")
        self._token = bot_token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")

    @property
    def _url(self) -> str:
        return f"{self._api_base}/bot{self._token}/sendMessage"

    def send(self, message: Message) -> None:
        for chunk in _split(message.text, self.max_length):
            response = self._post(
                self._url,
                payload={
                    "chat_id": self._chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                    "disable_notification": message.level == "info",
                },
            )
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = {}
            if isinstance(body, dict) and body.get("ok") is False:
                raise DeliveryError(
                    self.name, f"telegram rejected message: {body.get('description')}"
                )


def _split(text: str, limit: int) -> list[str]:
    """Split on newlines so no chunk exceeds the provider limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
            current = ""
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
        current += line
    if current:
        chunks.append(current)
    return chunks


_ = IST  # timezone is applied by the alert message itself
