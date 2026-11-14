"""Logging: secret redaction, JSON format, rotation target and audit trail."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from kite_alerts.core.models import Alert, Message, Side
from kite_alerts.notifiers.dlq import DeadLetterLog
from kite_alerts.observability.logging import (
    AuditLog,
    JsonFormatter,
    RedactingFilter,
    setup_logging,
)


def record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="kite_alerts.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class TestRedaction:
    def test_telegram_token_is_scrubbed(self) -> None:
        item = record("posting to https://api.telegram.org/bot123456:AAFakeTokenValue/sendMessage")
        RedactingFilter().filter(item)
        assert "AAFakeTokenValue" not in item.getMessage()

    def test_slack_webhook_is_scrubbed(self) -> None:
        item = record("hook https://hooks.slack.com/services/T000/B000/XXXX")
        RedactingFilter().filter(item)
        message = item.getMessage()
        assert "T000/B000/XXXX" not in message
        assert "hooks.slack.com" in message

    def test_assignments_are_scrubbed(self) -> None:
        item = record("config KITE_ACCESS_TOKEN=abc123secret")
        RedactingFilter().filter(item)
        assert "abc123secret" not in item.getMessage()

    def test_bearer_headers_are_scrubbed(self) -> None:
        item = record("Authorization: Bearer sometokenvalue")
        RedactingFilter().filter(item)
        assert "sometokenvalue" not in item.getMessage()

    def test_clean_messages_are_untouched(self) -> None:
        item = record("ALERT BUY NSE:TEST @ 100.00")
        RedactingFilter().filter(item)
        assert item.getMessage() == "ALERT BUY NSE:TEST @ 100.00"


class TestJsonFormatter:
    def test_line_is_valid_json(self) -> None:
        payload = json.loads(JsonFormatter().format(record("hello")))
        assert payload["msg"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "kite_alerts.test"
        assert "ts" in payload

    def test_extra_fields_are_included(self) -> None:
        item = record("alert")
        item.symbol = "NSE:TEST"
        payload = json.loads(JsonFormatter().format(item))
        assert payload["symbol"] == "NSE:TEST"

    def test_exception_is_rendered(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            item = logging.LogRecord(
                "kite_alerts", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        payload = json.loads(JsonFormatter().format(item))
        assert "boom" in payload["exc"]


class TestSetupLogging:
    def test_writes_to_a_rotating_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        try:
            path = setup_logging(level="DEBUG", log_dir=tmp_path, console=False)
            logging.getLogger("kite_alerts.test").info("written to the log file")
            for handler in root.handlers:
                handler.flush()
            assert path.exists()
            assert "written to the log file" in path.read_text(encoding="utf-8")
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
                handler.close()
            for handler in saved_handlers:
                root.addHandler(handler)
            root.setLevel(saved_level)

    def test_json_mode_writes_json_lines(self, tmp_path: Path) -> None:
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        try:
            path = setup_logging(level="INFO", log_dir=tmp_path, json_logs=True, console=False)
            logging.getLogger("kite_alerts.test").info("json line")
            for handler in root.handlers:
                handler.flush()
            first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            assert first["msg"] == "json line"
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
                handler.close()
            for handler in saved_handlers:
                root.addHandler(handler)
            root.setLevel(saved_level)

    def test_secrets_are_redacted_in_the_file(self, tmp_path: Path) -> None:
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        try:
            path = setup_logging(level="INFO", log_dir=tmp_path, console=False)
            logging.getLogger("kite_alerts.test").error("token KITE_ACCESS_TOKEN=supersecretvalue")
            for handler in root.handlers:
                handler.flush()
            assert "supersecretvalue" not in path.read_text(encoding="utf-8")
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
                handler.close()
            for handler in saved_handlers:
                root.addHandler(handler)
            root.setLevel(saved_level)


def make_alert() -> Alert:
    return Alert(
        symbol="NSE:TEST",
        side=Side.BUY,
        ts=pd.Timestamp("2026-11-16 10:00", tz="Asia/Kolkata"),
        price=100.5,
        reasons=("EMA9 crossed above EMA21", "RSI 62.0 in bullish zone"),
        rules_available=5,
        params={"min_agree": 4, "cooldown_minutes": 15},
        rules_used=("ema_cross", "rsi_zone"),
    )


class TestAuditLog:
    def test_records_an_alert_with_params(self, tmp_path: Path) -> None:
        audit = AuditLog(tmp_path / "logs" / "alerts-audit.jsonl")
        audit.record_alert(make_alert(), channels=["console"])
        entries = audit.tail(1)
        assert entries[0]["event"] == "alert_fired"
        assert entries[0]["symbol"] == "NSE:TEST"
        assert entries[0]["side"] == "BUY"
        assert entries[0]["params"]["min_agree"] == 4
        assert entries[0]["channels"] == ["console"]

    def test_records_lifecycle_events(self, tmp_path: Path) -> None:
        audit = AuditLog(tmp_path / "audit.jsonl")
        audit.record_event("started", symbols=["NSE:TEST"])
        assert audit.tail(1)[0]["event"] == "started"

    def test_tail_on_missing_file(self, tmp_path: Path) -> None:
        assert AuditLog(tmp_path / "absent.jsonl").tail() == []

    def test_tolerates_corrupt_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text('{"event": "ok"}\nnot json\n', encoding="utf-8")
        assert len(AuditLog(path).tail()) == 1


class TestDeadLetter:
    def test_records_payload_and_error(self, tmp_path: Path) -> None:
        dlq = DeadLetterLog(tmp_path / "dlq.jsonl")
        dlq.record(
            channel="telegram",
            message=Message(text="BUY NSE:TEST", level="alert", dedup_key="k1"),
            error="HTTP 500",
            attempts=4,
        )
        assert dlq.count == 1
        entry = dlq.tail(1)[0]
        assert entry["channel"] == "telegram"
        assert entry["text"] == "BUY NSE:TEST"
        assert entry["attempts"] == 4

    def test_tail_on_missing_file(self, tmp_path: Path) -> None:
        assert DeadLetterLog(tmp_path / "absent.jsonl").tail() == []
