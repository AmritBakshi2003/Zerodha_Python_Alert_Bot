"""Notifier interface and shared HTTP behaviour.

A notifier performs exactly one delivery attempt. Retry, rate limiting, dedup and
dead-lettering live in ``NotifierHub`` so every channel behaves identically.

Secrets never appear in exception messages: ``DeliveryError`` carries the channel
name and a redacted detail, because notifier errors end up in logs and the DLQ.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import requests

from ..core.models import Message

_TOKEN_IN_URL = re.compile(r"bot\d+:[A-Za-z0-9_-]{10,}")
_SLACK_IN_URL = re.compile(r"(https?://hooks\.slack\.com/services/)\S+")


def redact(text: str) -> str:
    """Remove credential-shaped substrings from arbitrary text."""
    text = _TOKEN_IN_URL.sub("bot<redacted>", text)
    return _SLACK_IN_URL.sub(r"\1<redacted>", text)


class DeliveryError(Exception):
    """A single delivery attempt failed (non-retryable or retries exhausted)."""

    def __init__(self, channel: str, detail: str, *, status: int | None = None) -> None:
        self.channel = channel
        self.detail = redact(detail)
        self.status = status
        super().__init__(f"{channel}: {self.detail}")


class RateLimitedError(DeliveryError):
    """The provider asked us to slow down; ``retry_after`` is honoured by the hub."""

    def __init__(self, channel: str, detail: str, *, retry_after: float) -> None:
        super().__init__(channel, detail, status=429)
        self.retry_after = max(0.0, float(retry_after))


@runtime_checkable
class Notifier(Protocol):
    name: str

    def send(self, message: Message) -> None:
        """Deliver one message or raise ``DeliveryError``."""

    def close(self) -> None: ...


class BaseNotifier(ABC):
    """Convenience base with a shared, timeout-configured ``requests`` session."""

    name = "base"
    max_length = 4000

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        session: requests.Session | None = None,
        sleep: Any = None,
    ) -> None:
        self.timeout = timeout
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._sleep = sleep

    def _post(
        self,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> requests.Response:
        try:
            response = self._session.post(
                url,
                json=dict(payload) if payload is not None else None,
                data=dict(data) if data is not None else None,
                headers=dict(headers) if headers is not None else None,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DeliveryError(self.name, f"request failed: {exc}") from exc

        if response.status_code == 429:
            raise RateLimitedError(
                self.name,
                "rate limited by provider",
                retry_after=_retry_after_seconds(response),
            )
        if response.status_code >= 400:
            raise DeliveryError(
                self.name,
                f"HTTP {response.status_code}: {response.text[:200]}",
                status=response.status_code,
            )
        return response

    def close(self) -> None:
        if self._owns_session:
            try:
                self._session.close()
            except Exception:  # pragma: no cover - shutdown path
                pass

    @abstractmethod
    def send(self, message: Message) -> None:
        """Channel-specific delivery."""


def _retry_after_seconds(response: requests.Response) -> float:
    """Header first, then Telegram's JSON ``parameters.retry_after``."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass  # some servers send an HTTP date; fall through
    try:
        body = response.json()
    except ValueError:
        return 5.0
    retry_after = (
        (body.get("parameters") or {}).get("retry_after") if isinstance(body, dict) else None
    )
    if isinstance(retry_after, bool):  # bool is an int subclass; not a delay
        return 5.0
    if isinstance(retry_after, (int, float)):
        return float(retry_after)
    if isinstance(retry_after, str):
        try:
            return float(retry_after)
        except ValueError:
            return 5.0
    return 5.0
