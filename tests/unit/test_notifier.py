"""Notification stack: channels, retry/backoff, dedup, DLQ, non-blocking hub."""

from __future__ import annotations

import json
import smtplib
from pathlib import Path
from typing import Any, ClassVar

import pytest
import requests

from fakes.fake_kite import RecordingNotifier
from kite_alerts.config.settings import Channel, NotifySettings
from kite_alerts.core.models import Alert, Message, Side
from kite_alerts.notifiers.base import DeliveryError, RateLimitedError, redact
from kite_alerts.notifiers.console import ConsoleNotifier
from kite_alerts.notifiers.dlq import DeadLetterLog
from kite_alerts.notifiers.hub import NotifierHub
from kite_alerts.notifiers.slack import SlackNotifier
from kite_alerts.notifiers.smtp import EmailNotifier
from kite_alerts.notifiers.telegram import TelegramNotifier
from kite_alerts.observability.metrics import Metrics

TELEGRAM_TOKEN = "123456:AAFakeTokenValueThatMustNeverLeak"
TELEGRAM_URL_PART = "AAFakeTokenValueThatMustNeverLeak"


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")
        self.headers = headers or {}

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


def telegram(session: FakeSession) -> TelegramNotifier:
    return TelegramNotifier(
        bot_token=TELEGRAM_TOKEN,
        chat_id="42",
        session=session,  # type: ignore[arg-type]
    )


def make_alert(key: str = "k") -> Alert:
    import pandas as pd

    return Alert(
        symbol="NSE:TEST",
        side=Side.BUY,
        ts=pd.Timestamp("2026-11-16 10:00", tz="Asia/Kolkata"),
        price=100.5,
        reasons=("EMA9 crossed above EMA21",),
    )


class TestTelegram:
    def test_successful_send(self) -> None:
        session = FakeSession([FakeResponse(200, {"ok": True})])
        telegram(session).send(Message(text="hello"))
        assert session.calls[0]["json"]["chat_id"] == "42"
        assert session.calls[0]["json"]["text"] == "hello"

    def test_rate_limit_header_is_honoured(self) -> None:
        session = FakeSession([FakeResponse(429, None, headers={"Retry-After": "7"})])
        with pytest.raises(RateLimitedError) as excinfo:
            telegram(session).send(Message(text="hello"))
        assert excinfo.value.retry_after == 7.0

    def test_telegram_body_retry_after_is_honoured(self) -> None:
        body = {"ok": False, "error_code": 429, "parameters": {"retry_after": 13}}
        session = FakeSession([FakeResponse(429, body)])
        with pytest.raises(RateLimitedError) as excinfo:
            telegram(session).send(Message(text="hello"))
        assert excinfo.value.retry_after == 13.0

    def test_non_numeric_retry_after_does_not_crash(self) -> None:
        session = FakeSession([FakeResponse(429, None, headers={"Retry-After": "Wed, 21 Oct"})])
        with pytest.raises(RateLimitedError) as excinfo:
            telegram(session).send(Message(text="hello"))
        assert excinfo.value.retry_after == 5.0

    def test_http_error_is_a_delivery_error_without_the_token(self) -> None:
        session = FakeSession([FakeResponse(500, {"ok": False}, text="boom")])
        with pytest.raises(DeliveryError) as excinfo:
            telegram(session).send(Message(text="hello"))
        assert TELEGRAM_URL_PART not in str(excinfo.value)

    def test_rejected_body_is_a_delivery_error(self) -> None:
        session = FakeSession([FakeResponse(200, {"ok": False, "description": "chat not found"})])
        with pytest.raises(DeliveryError, match="chat not found"):
            telegram(session).send(Message(text="hello"))

    def test_long_message_is_split(self) -> None:
        session = FakeSession([FakeResponse(200, {"ok": True}), FakeResponse(200, {"ok": True})])
        telegram(session).send(Message(text="x" * 5000))
        assert len(session.calls) == 2
        assert all(len(call["json"]["text"]) <= 4096 for call in session.calls)

    def test_malformed_token_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            TelegramNotifier(bot_token="nope", chat_id="1")

    def test_network_failure_is_a_delivery_error(self) -> None:
        session = FakeSession([requests.ConnectionError("no route")])
        with pytest.raises(DeliveryError, match="request failed"):
            telegram(session).send(Message(text="hello"))


class TestSlack:
    def test_success(self) -> None:
        session = FakeSession([FakeResponse(200, None, text="ok")])
        SlackNotifier(webhook_url="https://hooks.slack.com/services/T/B/X", session=session).send(
            Message(text="hello")
        )
        assert session.calls[0]["json"]["text"] == "hello"

    def test_rejection(self) -> None:
        session = FakeSession([FakeResponse(200, None, text="invalid_payload")])
        with pytest.raises(DeliveryError):
            SlackNotifier(
                webhook_url="https://hooks.slack.com/services/T/B/X", session=session
            ).send(Message(text="hello"))

    def test_invalid_url(self) -> None:
        with pytest.raises(ValueError):
            SlackNotifier(webhook_url="ftp://nope")

    def test_error_redacts_webhook(self) -> None:
        session = FakeSession(
            [FakeResponse(500, None, text="https://hooks.slack.com/services/T/B/X")]
        )
        with pytest.raises(DeliveryError) as excinfo:
            SlackNotifier(
                webhook_url="https://hooks.slack.com/services/T/B/X", session=session
            ).send(Message(text="hello"))
        assert "T/B/X" not in str(excinfo.value)


class TestEmail:
    class FakeSMTP:
        instances: ClassVar[list[Any]] = []
        fail = False

        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.host, self.port, self.timeout = host, port, timeout
            self.started_tls = False
            self.logged_in = False
            self.sent: list[Any] = []
            TestEmail.FakeSMTP.instances.append(self)

        def __enter__(self) -> TestEmail.FakeSMTP:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def ehlo(self) -> None:
            if TestEmail.FakeSMTP.fail:
                raise smtplib.SMTPException("server unhappy")

        def starttls(self, context: Any = None) -> None:
            self.started_tls = True

        def login(self, user: str, password: str) -> None:
            self.logged_in = True

        def send_message(self, email: Any) -> None:
            self.sent.append(email)

    def test_send_with_tls_and_login(self, monkeypatch: pytest.MonkeyPatch) -> None:
        TestEmail.FakeSMTP.fail = False
        TestEmail.FakeSMTP.instances.clear()
        monkeypatch.setattr(smtplib, "SMTP", TestEmail.FakeSMTP)
        notifier = EmailNotifier(
            host="smtp.example.com",
            port=587,
            sender="bot@example.com",
            recipients="a@example.com,b@example.com",
            username="user",
            password="pass",
        )
        notifier.send(Message(text="BUY NSE:TEST", subject="[kite-alerts] BUY"))
        client = TestEmail.FakeSMTP.instances[0]
        assert client.started_tls
        assert client.logged_in
        assert client.sent[0]["Subject"] == "[kite-alerts] BUY"
        assert "b@example.com" in client.sent[0]["To"]

    def test_failure_is_a_delivery_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        TestEmail.FakeSMTP.fail = True
        TestEmail.FakeSMTP.instances.clear()
        monkeypatch.setattr(smtplib, "SMTP", TestEmail.FakeSMTP)
        notifier = EmailNotifier(
            host="smtp.example.com", port=587, sender="bot@example.com", recipients="a@example.com"
        )
        with pytest.raises(DeliveryError):
            notifier.send(Message(text="hello"))


class TestConsole:
    def test_prints_the_alert_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleNotifier().send(Message(text="🟢 BUY NSE:TEST @ 100.00"))
        assert "BUY NSE:TEST" in capsys.readouterr().out


def hub_settings(**kwargs: Any) -> NotifySettings:
    defaults: dict[str, Any] = {
        "channels": (Channel.CONSOLE,),
        "retries": 3,
        "dedup_seconds": 300,
        "max_per_minute": 600,
    }
    defaults.update(kwargs)
    return NotifySettings(**defaults)


def make_hub(
    notifiers: list[Any], tmp_path: Path, **kwargs: Any
) -> tuple[NotifierHub, Metrics, DeadLetterLog]:
    metrics = Metrics()
    dlq = DeadLetterLog(tmp_path / "dead-letter.jsonl")
    hub = NotifierHub(
        notifiers,
        settings=hub_settings(**kwargs),
        metrics=metrics,
        dlq=dlq,
        sleep=lambda _seconds: None,
    )
    return hub, metrics, dlq


class TestHub:
    def test_retries_then_succeeds(self, tmp_path: Path) -> None:
        notifier = RecordingNotifier(fail_times=2)
        hub, mysql, _ = make_hub([notifier], tmp_path)
        hub.start()
        try:
            hub.submit(make_alert())
            assert hub.flush(timeout=5)
        finally:
            hub.close()
        assert notifier.attempts == 3
        assert len(notifier.messages) == 1
        assert mysql.get("alerts_sent") == 1

    def test_permanent_failure_goes_to_the_dead_letter_log(self, tmp_path: Path) -> None:
        notifier = RecordingNotifier(fail_times=99)
        hub, metrics, dlq = make_hub([notifier], tmp_path, retries=2)
        hub.start()
        try:
            hub.submit(make_alert())
            assert hub.flush(timeout=5)
        finally:
            hub.close()
        assert dlq.count == 1
        record = dlq.tail(1)[0]
        assert record["channel"] == "recording"
        assert "BUY" in record["text"]
        assert metrics.get("notify_dead_letters", channel="recording") == 1

    def test_duplicate_alerts_are_dropped(self, tmp_path: Path) -> None:
        notifier = RecordingNotifier()
        hub, _, _ = make_hub([notifier], tmp_path)
        hub.start()
        try:
            alert = make_alert()
            hub.submit(alert)
            hub.submit(alert)
            assert hub.flush(timeout=5)
        finally:
            hub.close()
        assert len(notifier.messages) == 1

    def test_dedup_can_be_disabled(self, tmp_path: Path) -> None:
        notifier = RecordingNotifier()
        hub, _, _ = make_hub([notifier], tmp_path, dedup_seconds=0)
        hub.start()
        try:
            alert = make_alert()
            hub.submit(alert)
            hub.submit(alert)
            assert hub.flush(timeout=5)
        finally:
            hub.close()
        assert len(notifier.messages) == 2

    def test_rate_limit_waits_for_retry_after(self, tmp_path: Path) -> None:
        class Limited:
            name = "limited"

            def __init__(self) -> None:
                self.attempts = 0
                self.messages: list[Message] = []

            def send(self, message: Message) -> None:
                self.attempts += 1
                if self.attempts == 1:
                    raise RateLimitedError(self.name, "slow down", retry_after=2.0)
                self.messages.append(message)

            def close(self) -> None:
                return None

        sleeps: list[float] = []
        notifier = Limited()
        hub = NotifierHub(
            [notifier],
            settings=hub_settings(),
            dlq=DeadLetterLog(tmp_path / "dlq.jsonl"),
            sleep=sleeps.append,
        )
        hub.start()
        try:
            hub.submit(make_alert())
            assert hub.flush(timeout=5)
        finally:
            hub.close()
        assert any(delay >= 2.0 for delay in sleeps)  # honoured retry_after
        assert len(notifier.messages) == 1

    def test_unexpected_exception_is_dead_lettered(self, tmp_path: Path) -> None:
        class Broken:
            name = "broken"

            def send(self, message: Message) -> None:
                raise RuntimeError("bug")

            def close(self) -> None:
                return None

        hub, _, dlq = make_hub([Broken()], tmp_path)
        hub.start()
        try:
            hub.submit(make_alert())
            assert hub.flush(timeout=5)
        finally:
            hub.close()
        assert dlq.count == 1

    def test_one_failing_channel_does_not_block_another(self, tmp_path: Path) -> None:
        bad = RecordingNotifier(name="bad", fail_times=99)
        good = RecordingNotifier(name="good")
        hub, _, _ = make_hub([bad, good], tmp_path, retries=2)
        hub.start()
        try:
            hub.submit(make_alert())
            assert hub.flush(timeout=5)
        finally:
            hub.close()
        assert len(good.messages) == 1

    def test_system_messages_are_not_counted_as_alerts(self, tmp_path: Path) -> None:
        notifier = RecordingNotifier()
        hub, metrics, _ = make_hub([notifier], tmp_path)
        hub.start()
        try:
            hub.send_text("✅ started", level="system")
            assert hub.flush(timeout=5)
        finally:
            hub.close()
        assert len(notifier.messages) == 1
        assert metrics.get("alerts_sent") == 0

    def test_no_notifiers_is_safe(self, tmp_path: Path) -> None:
        hub, _, _ = make_hub([], tmp_path)
        hub.submit(make_alert())  # must not raise
        hub.close()

    def test_health_snapshot(self, tmp_path: Path) -> None:
        hub, _, _ = make_hub([RecordingNotifier()], tmp_path)
        health = hub.health()
        assert health["channels"] == ["recording"]
        assert health["queue_depth"] == 0


class TestRedaction:
    def test_redact_removes_tokens_and_webhooks(self) -> None:
        text = f"url https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage and https://hooks.slack.com/services/T/B/X"
        cleaned = redact(text)
        assert TELEGRAM_URL_PART not in cleaned
        assert "T/B/X" not in cleaned
