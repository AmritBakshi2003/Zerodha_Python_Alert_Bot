"""Small cache with coalescing, exponential backoff and a circuit breaker.

Every provider adapter goes through this class. It is deliberately synchronous:
FastAPI runs these read-only routes in its threadpool, and the browser polls a
few times a minute at most. It never logs a provider response or URL.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from .types import SourceError

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CacheResult(Generic[T]):
    value: T
    age_seconds: float
    stale: bool = False
    error: SourceError | None = None
    retry_after: float = 0.0


@dataclass
class _Entry:
    value: Any
    fetched_at: float


@dataclass
class _Failure:
    count: int = 0
    next_retry_at: float = 0.0
    opened_until: float = 0.0
    message: str = ""
    code: str = "unavailable"


@dataclass
class ProviderCache:
    ttl: float = 8.0
    breaker_threshold: int = 3
    breaker_reset: float = 60.0
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    clock: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = random.random
    _entries: dict[str, _Entry] = field(default_factory=dict)
    _failures: dict[str, _Failure] = field(default_factory=dict)
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _meta_lock: threading.Lock = field(default_factory=threading.Lock)

    def _lock_for(self, key: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _backoff(self, failure: _Failure) -> float:
        raw = self.backoff_base * (2 ** max(0, failure.count - 1))
        return float(min(self.backoff_cap, raw * (0.5 + 0.5 * self.jitter())))

    def get(
        self,
        key: str,
        fetch: Callable[[], T],
        *,
        ttl: float | None = None,
    ) -> CacheResult[T]:
        """Return a fresh value, a stale value after failure, or raise SourceError."""
        with self._lock_for(key):
            now = self.clock()
            entry = self._entries.get(key)
            lifetime = self.ttl if ttl is None else ttl
            if entry is not None and now - entry.fetched_at < lifetime:
                return CacheResult(value=entry.value, age_seconds=now - entry.fetched_at)

            failure = self._failures.get(key)
            if failure is not None:
                if failure.opened_until and now < failure.opened_until:
                    delay = max(0.0, failure.opened_until - now)
                    return self._stale_or_raise(key, entry, failure, now, delay)
                if now < failure.next_retry_at:
                    return self._stale_or_raise(
                        key, entry, failure, now, failure.next_retry_at - now
                    )

            try:
                value = fetch()
            except SourceError as exc:
                failure = failure or _Failure()
                failure.count += 1
                failure.message = exc.message
                failure.code = exc.code
                delay = exc.retry_after or self._backoff(failure)
                failure.next_retry_at = now + delay
                if failure.count >= self.breaker_threshold:
                    failure.opened_until = now + self.breaker_reset
                    log.warning("provider %s circuit opened after %d failures", key, failure.count)
                self._failures[key] = failure
                return self._stale_or_raise(key, entry, failure, now, delay)
            except Exception as exc:  # providers must never leak their exception text
                failure = failure or _Failure()
                failure.count += 1
                failure.message = "the data provider failed"
                failure.code = "unavailable"
                delay = self._backoff(failure)
                failure.next_retry_at = now + delay
                if failure.count >= self.breaker_threshold:
                    failure.opened_until = now + self.breaker_reset
                self._failures[key] = failure
                log.warning("provider %s failed (%s)", key, type(exc).__name__)
                return self._stale_or_raise(key, entry, failure, now, delay)

            self._entries[key] = _Entry(value=value, fetched_at=now)
            self._failures.pop(key, None)
            return CacheResult(value=value, age_seconds=0.0)

    def _stale_or_raise(
        self,
        key: str,
        entry: _Entry | None,
        failure: _Failure,
        now: float,
        delay: float,
    ) -> CacheResult[Any]:
        error = SourceError(failure.message, retry_after=delay, code=failure.code)
        if entry is not None:
            return CacheResult(
                value=entry.value,
                age_seconds=now - entry.fetched_at,
                stale=True,
                error=error,
                retry_after=delay,
            )
        raise error

    def clear(self) -> None:
        with self._meta_lock:
            self._entries.clear()
            self._failures.clear()
