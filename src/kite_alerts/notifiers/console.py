"""Console notifier: the prototype's behaviour, now one channel among many."""

from __future__ import annotations

import sys
from typing import IO

from ..core.models import Message
from .base import BaseNotifier


def ensure_utf8_stream(stream: IO[str] | None = None, *, newline: str | None = None) -> IO[str]:
    """Force a stream to UTF-8.

    Alert text contains emoji. On Windows the default console encoding is cp1252,
    so printing an alert to a redirected stdout raised ``UnicodeEncodeError`` and
    the alert was lost. Reconfiguring the stream makes the demo and the console
    channel behave identically on every platform.
    """
    target = stream if stream is not None else sys.stdout
    reconfigure = getattr(target, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace", newline=newline)
        except (ValueError, OSError):  # pragma: no cover - already consumed stream
            pass
    return target


class ConsoleNotifier(BaseNotifier):
    name = "console"

    def __init__(self, *, stream: IO[str] | None = None) -> None:
        super().__init__()
        self._stream = ensure_utf8_stream(stream) if stream is None else stream

    def send(self, message: Message) -> None:
        print(message.text, file=self._stream, flush=True)

    def close(self) -> None:
        return None
