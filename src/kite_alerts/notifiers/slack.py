"""Slack incoming-webhook notifier."""

from __future__ import annotations

import requests

from ..core.models import Message
from .base import BaseNotifier, DeliveryError

SLACK_LIMIT = 3000


class SlackNotifier(BaseNotifier):
    name = "slack"
    max_length = SLACK_LIMIT

    def __init__(
        self,
        *,
        webhook_url: str,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(timeout=timeout, session=session)
        if not webhook_url.startswith(("https://", "http://")):
            raise ValueError("slack webhook URL must be an http(s) URL")
        self._webhook_url = webhook_url

    def send(self, message: Message) -> None:
        response = self._post(
            self._webhook_url,
            payload={"text": message.text[: self.max_length]},
        )
        body = response.text.strip().lower()
        if body and body != "ok":
            raise DeliveryError(self.name, f"slack rejected message: {body[:120]}")
