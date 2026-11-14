"""Dead-letter log for undeliverable alerts.

The prototype returned ``False`` from a failed send and nobody looked, so an
alert could disappear with no trace. Every final failure is now appended to a
JSONL file with the payload, channel, attempts and error - re-sendable by hand.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from ..core.clock import to_ist_ts
from ..core.models import Message

log = logging.getLogger(__name__)


class DeadLetterLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._count = 0

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def record(
        self,
        *,
        channel: str,
        message: Message,
        error: BaseException | str,
        attempts: int,
    ) -> None:
        record: dict[str, Any] = {
            "ts": to_ist_ts(_now()).isoformat(),
            "channel": channel,
            "level": message.level,
            "dedup_key": message.dedup_key,
            "subject": message.subject,
            "text": message.text,
            "attempts": attempts,
            "error": str(error)[:1000],
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._count += 1
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            except OSError:
                # Last resort: the payload must still reach the operator's log.
                log.error("DEAD LETTER (cannot write %s): %s", self.path, line)

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def _now() -> Any:
    import datetime as dt

    from ..core.clock import IST

    return dt.datetime.now(tz=IST)
