"""Logging setup and the separate alerts audit log.

* Structured, optionally JSON, to stdout and a rotating file.
* A redaction filter that strips bot tokens / webhook URLs / access tokens, so a
  requested log file can be handed to a client without leaking credentials.
* ``logs/alerts-audit.jsonl``: one JSON object per fired alert including the
  parameter snapshot, independent of the application log (and never rotated away
  with debug noise).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import threading
from pathlib import Path
from typing import Any

from ..core.clock import to_ist_ts
from ..core.models import Alert

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"bot\d+:[A-Za-z0-9_-]{10,}"), "bot<redacted>"),
    (re.compile(r"(https?://hooks\.slack\.com/services/)\S+"), r"\1<redacted>"),
    (
        re.compile(r"(?i)(access_token|api_secret|api_key|password)(\s*[=:]\s*)\S+"),
        r"\1\2<redacted>",
    ),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1<redacted>"),
)


class RedactingFilter(logging.Filter):
    """Scrub credential-shaped substrings from log messages and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            return True
        scrubbed = message
        for pattern, replacement in _REDACTIONS:
            scrubbed = pattern.sub(replacement, scrubbed)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, IST timestamp, exception as text."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _iso(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key in ("symbol", "channel", "event", "alert_key"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def _iso(created: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(created, tz=dt.timezone.utc).astimezone().isoformat()


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: str | Path = "logs",
    json_logs: bool = False,
    filename: str = "kite-alerts.log",
    console: bool = True,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 5,
) -> Path:
    """Configure root logging once; returns the log file path."""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / filename

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter: logging.Formatter
    if json_logs:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
        )

    redactor = RedactingFilter()
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        root.addHandler(stream)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("kite_alerts").setLevel(getattr(logging, level.upper(), logging.INFO))
    # Third-party noise.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("twisted").setLevel(logging.WARNING)
    logging.getLogger("autobahn").setLevel(logging.WARNING)
    return log_file


class AuditLog:
    """Append-only JSONL audit trail for alerts (and lifecycle events)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record_alert(self, alert: Alert, *, channels: list[str] | None = None) -> None:
        record = alert.audit_record()
        record["event"] = "alert_fired"
        record["channels"] = channels or []
        self._write(record)

    def record_event(self, event: str, **fields: Any) -> None:
        record = {"ts": to_ist_ts(_now()).isoformat(), "event": event}
        record.update(fields)
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            except OSError:
                logging.getLogger(__name__).exception("cannot write audit log %s", self.path)

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        out: list[dict[str, Any]] = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def _now() -> Any:
    import datetime as dt

    from ..core.clock import IST

    return dt.datetime.now(tz=IST)
