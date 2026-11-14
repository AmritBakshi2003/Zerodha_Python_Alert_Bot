"""Notifier factory: one channel object per configured channel."""

from __future__ import annotations

import pytest

from kite_alerts.config.settings import Channel, NotifySettings
from kite_alerts.notifiers.console import ConsoleNotifier
from kite_alerts.notifiers.factory import build_notifiers
from kite_alerts.notifiers.slack import SlackNotifier
from kite_alerts.notifiers.smtp import EmailNotifier
from kite_alerts.notifiers.telegram import TelegramNotifier


def full_settings(**kwargs) -> NotifySettings:
    defaults = {
        "channels": (Channel.CONSOLE, Channel.TELEGRAM, Channel.SLACK, Channel.SMTP),
        "telegram_bot_token": "123456:AAFakeTokenValue",
        "telegram_chat_id": "-100200",
        "slack_webhook_url": "https://hooks.slack.com/services/T/B/X",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_from": "bot@example.com",
        "smtp_to": "ops@example.com",
        "smtp_user": "user",
        "smtp_password": "pass",
    }
    defaults.update(kwargs)
    return NotifySettings(**defaults)


def test_builds_every_configured_channel() -> None:
    notifiers = build_notifiers(full_settings())
    assert [n.name for n in notifiers] == ["console", "telegram", "slack", "smtp"]
    assert isinstance(notifiers[0], ConsoleNotifier)
    assert isinstance(notifiers[1], TelegramNotifier)
    assert isinstance(notifiers[2], SlackNotifier)
    assert isinstance(notifiers[3], EmailNotifier)


def test_console_only() -> None:
    notifiers = build_notifiers(NotifySettings(channels=(Channel.CONSOLE,)))
    assert [n.name for n in notifiers] == ["console"]


def test_empty_channel_tuple_builds_nothing() -> None:
    assert build_notifiers(NotifySettings(channels=())) == []


def test_timeout_is_forwarded() -> None:
    notifiers = build_notifiers(full_settings(timeout_seconds=7.5))
    assert notifiers[1].timeout == 7.5


def test_channels_are_closed_safely() -> None:
    for notifier in build_notifiers(full_settings()):
        notifier.close()


def test_invalid_channel_configuration_is_rejected_by_settings() -> None:
    with pytest.raises(Exception):
        NotifySettings(channels=(Channel.TELEGRAM,))
