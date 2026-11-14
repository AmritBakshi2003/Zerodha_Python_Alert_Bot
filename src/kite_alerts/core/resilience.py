"""Resilience primitives: bounded retry with jitter, rate limiting, circuit breaker.

The prototype retried with a fixed ``base * 2**i`` sleep, no cap, no jitter, no
shared budget and no breaker. All four matter in production: a thundering retry
against Kite's historical endpoint gets the token rate-limited, which then looks
like a dead feed.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from .errors import CircuitOpenError, KiteAlertsError, classify_exception

T = TypeVar("T")
BreakerState = Literal["closed", "open", "half_open"]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff with full jitter."""

    retries: int = 5
    base_seconds: float = 1.0
    cap_seconds: float = 30.0
    jitter: float = 0.5

    def delay_for(self, attempt: int, rand: Callable[[], float] = random.random) -> float:
        """Delay before retry ``attempt`` (0-based), capped, with jitter."""
        raw = min(self.cap_seconds, self.base_seconds * (2.0**attempt))
        return min(self.cap_seconds, raw + rand() * raw * self.jitter)


def retry_call(
    fn: Callable[..., T],
    *args: Any,
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
    classify: Callable[[BaseException], str] = classify_exception,
    on_retry: Callable[[int, BaseException, float, str], None] | None = None,
    rand: Callable[[], float] = random.random,
    **kwargs: Any,
) -> T:
    """Call ``fn`` with bounded retries.

    Auth and fatal errors are re-raised immediately: retrying cannot fix an
    expired token or a malformed symbol.
    """
    if policy.retries < 1:
        raise ValueError("policy.retries must be >= 1")

    last: BaseException | None = None
    for attempt in range(policy.retries):
        try:
            return fn(*args, **kwargs)
        except KiteAlertsError as exc:
            # Our own typed errors carry a deliberate decision; never retry them.
            last = exc
            raise
        except Exception as exc:
            kind = classify(exc)
            last = exc
            if kind in ("auth", "fatal") or attempt == policy.retries - 1:
                raise
            delay = policy.delay_for(attempt, rand)
            if on_retry is not None:
                on_retry(attempt, exc, delay, kind)
            sleep(delay)
    if last is not None:  # pragma: no cover - the loop returns or raises
        raise last
    raise RuntimeError("retry loop exited without a result")  # pragma: no cover


class TokenBucket:
    """Thread-safe token bucket (default: Kite historical's ~3 requests/second)."""

    def __init__(
        self,
        rate_per_sec: float,
        capacity: float | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate_per_sec))
        self._tokens = self.capacity
        self._monotonic = monotonic
        self._sleep = sleep
        self._last = monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available; return the seconds waited."""
        if tokens > self.capacity:
            tokens = self.capacity
        with self._lock:
            now = self._monotonic()
            self._tokens = min(self.capacity, self._tokens + max(0.0, now - self._last) * self.rate)
            self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            needed = (tokens - self._tokens) / self.rate
            self._tokens = 0.0
            self._last = now + needed
        self._sleep(needed)
        return needed


class CircuitBreaker:
    """Minimal breaker: closed -> open after N failures -> half-open trial."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 120.0,
        name: str = "dependency",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._monotonic = monotonic
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open = False
        self._lock = threading.Lock()

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> BreakerState:
        if self._opened_at is None:
            return "closed"
        if self._monotonic() - self._opened_at >= self.reset_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        with self._lock:
            state = self._state_locked()
            if state == "closed":
                return True
            if state == "open":
                return False
            if self._half_open:
                return False  # one trial at a time
            self._half_open = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._half_open = False

    def record_failure(self) -> None:
        with self._lock:
            self._half_open = False
            self._failures += 1
            if self._opened_at is None and self._failures >= self.failure_threshold:
                self._opened_at = self._monotonic()
            elif self._opened_at is not None:
                # A failed half-open trial re-opens the breaker immediately.
                self._opened_at = self._monotonic()

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if not self.allow():
            raise CircuitOpenError(f"{self.name} circuit is open; refusing call")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result
