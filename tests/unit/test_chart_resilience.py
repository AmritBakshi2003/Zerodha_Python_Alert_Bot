"""Cache, coalescing, backoff and circuit-breaker behaviour (no network)."""

from __future__ import annotations

import pytest

from kite_alerts.ui.chart.resilience import ProviderCache
from kite_alerts.ui.chart.types import SourceError


class TestProviderCache:
    def test_fresh_value_is_reused_until_ttl(self) -> None:
        now = [100.0]
        cache = ProviderCache(ttl=5.0, clock=lambda: now[0], jitter=lambda: 0.0)
        calls = []

        def fetch() -> str:
            calls.append(1)
            return "value"

        assert cache.get("k", fetch).value == "value"
        now[0] = 104.0
        assert cache.get("k", fetch).value == "value"
        assert len(calls) == 1
        now[0] = 106.0
        assert cache.get("k", fetch).value == "value"
        assert len(calls) == 2

    def test_failure_keeps_stale_value_and_reports_retry(self) -> None:
        now = [100.0]
        cache = ProviderCache(
            ttl=1.0,
            clock=lambda: now[0],
            jitter=lambda: 0.0,
            backoff_base=2.0,
        )
        cache.get("k", lambda: "old")
        now[0] = 102.0

        def fail():
            raise SourceError("temporarily unavailable", retry_after=7.0)

        result = cache.get("k", fail)
        assert result.value == "old"
        assert result.stale is True
        assert result.error is not None
        assert result.error.message == "temporarily unavailable"
        assert result.retry_after == 7.0

    def test_backoff_doubles_with_jitter_and_is_capped(self) -> None:
        now = [0.0]
        cache = ProviderCache(
            ttl=0.0,
            clock=lambda: now[0],
            jitter=lambda: 0.0,
            backoff_base=2.0,
            backoff_cap=5.0,
        )

        def fail():
            raise SourceError("down")

        cache.get("k", lambda: "old")
        first = cache.get("k", fail)
        assert first.retry_after == 1.0  # 2 * (0.5 + 0.0)
        now[0] = 1.0
        second = cache.get("k", fail)
        assert second.retry_after == 2.0
        now[0] = 3.1
        third = cache.get("k", fail)
        assert third.retry_after == 4.0
        now[0] = 7.3
        # threshold opens the circuit instead of a further attempt
        fourth = cache.get("k", fail)
        assert fourth.retry_after == pytest.approx(55.8)

    def test_no_previous_value_raises_a_plain_error(self) -> None:
        cache = ProviderCache(ttl=0.0, jitter=lambda: 0.0)

        def fail():
            raise SourceError("provider is down")

        with pytest.raises(SourceError) as caught:
            cache.get("new", fail)
        assert caught.value.message == "provider is down"
        assert "http" not in caught.value.message.lower()

    def test_breaker_stops_calling_the_provider(self) -> None:
        now = [0.0]
        cache = ProviderCache(
            ttl=0.0,
            clock=lambda: now[0],
            jitter=lambda: 0.0,
            breaker_threshold=2,
            breaker_reset=60.0,
        )
        calls = []

        def fail():
            calls.append(1)
            raise SourceError("down")

        with pytest.raises(SourceError):
            cache.get("k", fail)
        now[0] = 1.0
        with pytest.raises(SourceError):
            cache.get("k", fail)
        assert len(calls) == 2
        now[0] = 2.0
        with pytest.raises(SourceError):
            cache.get("k", fail)
        assert len(calls) == 2  # the circuit is open

    def test_unexpected_exception_is_never_exposed(self) -> None:
        cache = ProviderCache(ttl=0.0, jitter=lambda: 0.0)

        def explode():
            raise RuntimeError("secret provider URL https://example.invalid")

        with pytest.raises(SourceError) as caught:
            cache.get("k", explode)
        assert "example.invalid" not in caught.value.message
        assert "secret" not in caught.value.message

    def test_clear_resets_entries_and_failures(self) -> None:
        cache = ProviderCache(ttl=100.0)
        cache.get("k", lambda: 1)
        cache.clear()
        assert cache.get("k", lambda: 2).value == 2
