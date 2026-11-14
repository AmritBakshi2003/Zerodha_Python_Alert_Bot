"""Typed error hierarchy plus a single place that classifies Kite exceptions.

The prototype matched on ``type(e).__name__`` inline and treated an unknown
symbol (``InputException``) as fatal, which aborted startup for every symbol.
Classification lives here so retry/abort/alert decisions are consistent.
"""

from __future__ import annotations

from typing import Literal

ErrorKind = Literal["auth", "rate_limit", "retryable", "fatal"]


class KiteAlertsError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(KiteAlertsError):
    """Invalid or missing configuration. Message must be actionable."""


class AuthError(KiteAlertsError):
    """Kite access token is missing, expired or revoked.

    Recoverable only by a human running ``kite-alerts login``, so the runner
    alerts once and exits instead of retrying.
    """


class DataUnavailableError(KiteAlertsError):
    """Historical data or instrument metadata could not be obtained."""


class CircuitOpenError(KiteAlertsError):
    """A dependency is failing repeatedly; calls are refused while the breaker is open."""


class NotificationError(KiteAlertsError):
    """A channel failed to deliver after exhausting its retries."""


class OrderApiForbiddenError(KiteAlertsError):
    """Defensive: raised if anything ever attempts an order-side API call."""


TOKEN_ERROR_NAMES = frozenset({"TokenException", "PermissionException", "UserException"})
RATE_LIMIT_MARKERS = ("429", "too many request", "rate limit", "rate-limit")
RETRYABLE_NAMES = frozenset(
    {
        "NetworkException",
        "DataException",
        "GeneralException",
        "Timeout",
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
        "RemoteDisconnected",
    }
)


def classify_exception(exc: BaseException) -> ErrorKind:
    """Classify an exception raised by the Kite client (or a transport)."""
    name = type(exc).__name__
    message = str(exc).lower()

    if name in TOKEN_ERROR_NAMES:
        return "auth"
    if any(marker in message for marker in RATE_LIMIT_MARKERS):
        return "rate_limit"
    if name in RETRYABLE_NAMES:
        return "retryable"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "retryable"
    return "fatal"
