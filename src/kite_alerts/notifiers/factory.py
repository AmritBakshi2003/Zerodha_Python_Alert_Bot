"""Build notifiers from validated configuration."""

from __future__ import annotations

import logging

import requests

from ..config.settings import Channel, NotifySettings
from .base import Notifier
from .console import ConsoleNotifier
from .slack import SlackNotifier
from .smtp import EmailNotifier
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)


def build_notifiers(
    settings: NotifySettings, *, session: requests.Session | None = None
) -> list[Notifier]:
    notifiers: list[Notifier] = []
    for channel in settings.channels:
        if channel is Channel.CONSOLE:
            notifiers.append(ConsoleNotifier())
        elif channel is Channel.TELEGRAM:
            notifiers.append(
                TelegramNotifier(
                    bot_token=settings.telegram_bot_token.get_secret_value(),
                    chat_id=settings.telegram_chat_id,
                    timeout=settings.timeout_seconds,
                    session=session,
                )
            )
        elif channel is Channel.SLACK:
            notifiers.append(
                SlackNotifier(
                    webhook_url=settings.slack_webhook_url.get_secret_value(),
                    timeout=settings.timeout_seconds,
                    session=session,
                )
            )
        elif channel is Channel.SMTP:
            notifiers.append(
                EmailNotifier(
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    sender=settings.smtp_from,
                    recipients=settings.smtp_to,
                    username=settings.smtp_user,
                    password=settings.smtp_password.get_secret_value(),
                    use_tls=settings.smtp_use_tls,
                )
            )
        else:  # pragma: no cover - enum is exhaustive
            log.warning("unknown notification channel %s", channel)
    return notifiers
