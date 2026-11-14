"""The "send test alert" action.

One click sends a clearly labelled TEST message through every configured channel,
once, and reports the outcome per channel in plain language. It uses the same
notifier classes as the bot but not the retry/dead-letter hub: a test is
diagnostic, so an immediate per-channel answer is more useful than hidden
retries.

Hard rules:
* a test message is never stored in the alert history (there is no store here);
* the response never contains a token, chat id or webhook URL;
* at most one test every :data:`RATE_LIMIT_SECONDS`, so a stuck finger cannot
  spam a phone.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config.settings import NotifySettings, Settings
from ..core.models import Message
from ..notifiers.base import Notifier, redact
from ..notifiers.console import ConsoleNotifier
from ..notifiers.factory import build_notifiers

RATE_LIMIT_SECONDS = 10
TEST_TEXT = (
    "🧪 TEST alert from the kite-alerts dashboard. This is only a test message, "
    "not a trade signal. (alerts only — no orders were placed)"
)

CHANNEL_LABELS = {
    "console": "This terminal window",
    "telegram": "Telegram",
    "slack": "Slack",
    "smtp": "Email (SMTP)",
}

TELEGRAM_SETUP = (
    "Telegram is not set up. Create a bot with @BotFather, put TELEGRAM_BOT_TOKEN and "
    "TELEGRAM_CHAT_ID in .env, add telegram to NOTIFY_CHANNELS, then restart the bot."
)
SLACK_SETUP = (
    "Slack is not set up. Create an Incoming Webhook, put SLACK_WEBHOOK_URL in .env, "
    "add slack to NOTIFY_CHANNELS, then restart the bot."
)

NotifierFactory = Callable[[NotifySettings], "list[Notifier]"]


@dataclass(frozen=True, slots=True)
class ChannelResult:
    channel: str
    label: str
    ok: bool
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "label": self.label,
            "ok": self.ok,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class TestAlertOutcome:
    ok: bool
    label: str
    detail: str
    results: tuple[ChannelResult, ...] = ()
    setup: tuple[str, ...] = ()
    retry_after: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "label": self.label,
            "detail": self.detail,
            "results": [result.as_dict() for result in self.results],
            "setup": list(self.setup),
            "retry_after": round(self.retry_after, 1),
        }


@dataclass
class TestAlertService:
    """Rate-limited, synchronous test delivery with per-channel results."""

    settings: Settings | None
    settings_error: str | None = None
    notifier_factory: NotifierFactory = build_notifiers
    clock: Callable[[], float] = time.monotonic
    min_interval: float = RATE_LIMIT_SECONDS
    _last_sent: float | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def send(self) -> TestAlertOutcome:
        if self.settings is None:
            detail = "Your settings could not be read, so no channels are available."
            if self.settings_error:
                detail = f"{detail} {self.settings_error}"
            return TestAlertOutcome(
                ok=False,
                label="Could not send the test alert",
                detail=detail,
                setup=(
                    "Fix the .env problem above, then restart the dashboard with "
                    "`kite-alerts ui`.",
                ),
            )

        with self._lock:
            now = self.clock()
            if self._last_sent is not None:
                remaining = self.min_interval - (now - self._last_sent)
                if remaining > 0:
                    return TestAlertOutcome(
                        ok=False,
                        label="Please wait a moment",
                        detail=(
                            "Test alerts are limited to one every "
                            f"{int(self.min_interval)} seconds. Try again in "
                            f"{max(1, int(remaining + 0.999))} seconds."
                        ),
                        retry_after=remaining,
                    )
            self._last_sent = now
            return self._deliver()

    # ------------------------------------------------------------------ delivery
    def _deliver(self) -> TestAlertOutcome:
        assert self.settings is not None  # guarded by send()
        notifiers = self.notifier_factory(self.settings.notify)
        if not any(notifier.name == "console" for notifier in notifiers):
            notifiers.insert(0, ConsoleNotifier())

        message = Message(
            text=TEST_TEXT,
            level="info",
            subject="[kite-alerts] TEST message",
        )
        results: list[ChannelResult] = []
        for notifier in notifiers:
            label = CHANNEL_LABELS.get(notifier.name, notifier.name.replace("_", " ").title())
            try:
                notifier.send(message)
            except Exception as exc:  # each channel is independent
                detail = redact(str(exc))[:180]
                results.append(
                    ChannelResult(
                        channel=notifier.name,
                        label=label,
                        ok=False,
                        message=f"{label}: could not send. {detail}",
                    )
                )
            else:
                results.append(
                    ChannelResult(
                        channel=notifier.name,
                        label=label,
                        ok=True,
                        message=f"{label}: test message sent.",
                    )
                )
            finally:
                # A close failure is never actionable for a test message.
                with contextlib.suppress(Exception):  # pragma: no cover
                    notifier.close()

        configured = {notifier.name for notifier in notifiers}
        setup: list[str] = []
        if "telegram" not in configured:
            setup.append(TELEGRAM_SETUP)
        if "slack" not in configured:
            setup.append(SLACK_SETUP)

        failed = [result for result in results if not result.ok]
        if not failed:
            label = "Test alert sent"
            detail = (
                "Check each channel below. Test messages are never saved to your alert history."
            )
            if configured == {"console"}:
                detail = (
                    "Only the console channel is set up, so the test appeared in the terminal "
                    "window running the dashboard. Add Telegram or Slack below to reach your phone."
                )
        else:
            label = "Test alert partly failed"
            detail = (
                f"{len(failed)} of {len(results)} channel(s) could not send. "
                "The other channels still received the test."
            )
        return TestAlertOutcome(
            ok=not failed,
            label=label,
            detail=detail,
            results=tuple(results),
            setup=tuple(setup),
        )
