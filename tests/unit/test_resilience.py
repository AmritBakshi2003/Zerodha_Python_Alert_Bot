"""Retry, rate limiting and circuit breaking."""

from __future__ import annotations

import pytest

from fakes.fake_kite import TokenException
from kite_alerts.core.errors import CircuitOpenError
from kite_alerts.core.resilience import CircuitBreaker, RetryPolicy, TokenBucket, retry_call


class TestRetry:
    def test_succeeds_after_transient_failures(self) -> None:
        attempts: list[int] = []
        sleeps: list[float] = []

        def flaky() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                from fakes.fake_kite import NetworkException

                raise NetworkException("temporary")
            return "ok"

        result = retry_call(
            flaky, policy=RetryPolicy(retries=4), sleep=sleeps.append, rand=lambda: 0.0
        )
        assert result == "ok"
        assert len(attempts) == 3
        assert len(sleeps) == 2

    def test_auth_error_is_not_retried(self) -> None:
        calls: list[int] = []

        def expired() -> None:
            calls.append(1)
            raise TokenException("token expired")

        with pytest.raises(TokenException):
            retry_call(expired, policy=RetryPolicy(retries=5), sleep=lambda _: None)
        assert len(calls) == 1

    def test_exhausts_and_raises(self) -> None:
        sleeps: list[float] = []

        def always() -> None:
            from fakes.fake_kite import NetworkException

            raise NetworkException("down")

        with pytest.raises(Exception, match="down"):
            retry_call(always, policy=RetryPolicy(retries=3), sleep=sleeps.append, rand=lambda: 0.0)
        assert len(sleeps) == 2

    def test_backoff_is_capped_and_jittered(self) -> None:
        policy = RetryPolicy(retries=6, base_seconds=1.0, cap_seconds=4.0, jitter=0.0)
        assert [policy.delay_for(i, lambda: 0.0) for i in range(6)] == [1, 2, 4, 4, 4, 4]
        jittered = policy.delay_for(1, lambda: 1.0)
        assert jittered == pytest.approx(2.0 + 2.0 * policy.jitter)

    def test_invalid_retries(self) -> None:
        with pytest.raises(ValueError):
            retry_call(lambda: None, policy=RetryPolicy(retries=0))


class TestTokenBucket:
    def test_burst_then_wait(self) -> None:
        now = [0.0]
        slept: list[float] = []

        bucket = TokenBucket(
            rate_per_sec=2.0,
            capacity=2.0,
            monotonic=lambda: now[0],
            sleep=slept.append,
        )
        assert bucket.acquire() == 0.0
        assert bucket.acquire() == 0.0
        waited = bucket.acquire()
        assert waited == pytest.approx(0.5)
        assert slept == [pytest.approx(0.5)]

    def test_refills_over_time(self) -> None:
        now = [0.0]
        bucket = TokenBucket(
            rate_per_sec=10.0, capacity=1.0, monotonic=lambda: now[0], sleep=lambda _: None
        )
        bucket.acquire()
        now[0] = 1.0
        assert bucket.acquire() == 0.0

    def test_invalid_rate(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate_per_sec=0)


class TestCircuitBreaker:
    def test_opens_after_threshold_and_recovers(self) -> None:
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60, monotonic=lambda: now[0])
        for _ in range(3):
            assert breaker.allow()
            breaker.record_failure()
        assert breaker.state == "open"
        assert not breaker.allow()

        now[0] = 61.0
        assert breaker.state == "half_open"
        assert breaker.allow()  # one trial
        assert not breaker.allow()  # only one at a time
        breaker.record_success()
        assert breaker.state == "closed"
        assert breaker.allow()

    def test_failed_half_open_reopens(self) -> None:
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10, monotonic=lambda: now[0])
        breaker.record_failure()
        now[0] = 11.0
        assert breaker.allow()
        breaker.record_failure()
        assert breaker.state == "open"

    def test_call_wrapper(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10)
        assert breaker.call(lambda x: x + 1, 1) == 2

        def boom() -> None:
            raise ValueError("nope")

        with pytest.raises(ValueError):
            breaker.call(boom)
        with pytest.raises(CircuitOpenError):
            breaker.call(lambda: 1)

    def test_invalid_threshold(self) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker(failure_threshold=0)
