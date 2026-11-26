"""F22: shared feed state must follow one lock discipline.

``_bootstrap_data`` is written by bootstrap threads and read by request threads;
before this fix the read handed callers the *live* list, so one request could
corrupt the cache for every later request. ``_last_push`` is read-modify-written
on the ticker path. The lock order is ``_lock -> _sse_lock -> builder._lock``.
"""

from __future__ import annotations

import threading

import pandas as pd

from fakes.fake_kite import FakeKite, FakeTicker
from kite_alerts.core.clock import IST
from kite_alerts.ui.live_feed import LiveFeedService, LiveTick

EXCHANGE_TS = pd.Timestamp("2026-11-16 10:00", tz=IST).timestamp()


def _make_feed() -> LiveFeedService:
    FakeTicker.reset()
    return LiveFeedService(
        ticker_factory=lambda key, token: FakeTicker(key, token),
        kite_factory=lambda: FakeKite(),
        is_simulated=True,
    )


def _tick(symbol: str, token: int, price: float = 100.0) -> LiveTick:
    return LiveTick(
        symbol=symbol,
        token=token,
        price=price,
        volume_cum=1_000,
        oi=0.0,
        exchange_ts=EXCHANGE_TS,
        received_at=EXCHANGE_TS + 0.05,
        is_simulated=True,
    )


def test_bootstrap_snapshot_is_not_mutable_by_callers() -> None:
    """A reader must not be able to corrupt the bootstrap cache (F22)."""
    feed = _make_feed()
    feed._bootstrap_data["NSE:TEST"] = [{"ts": 1}]

    first = feed.bootstrap_data_for("NSE:TEST")
    first.append({"ts": 2})

    assert feed.bootstrap_data_for("NSE:TEST") == [{"ts": 1}]


def test_concurrent_ticks_and_bootstrap_reads_stay_consistent() -> None:
    """4 ticker threads + 4 request threads: no exceptions, complete state (F22)."""
    feed = _make_feed()
    feed._bootstrap_data["NSE:TEST"] = [{"ts": 1}]
    symbols = [f"NSE:RACE{i}" for i in range(4)]
    barrier = threading.Barrier(len(symbols) + 2)
    errors: list[BaseException] = []

    def ticker(symbol: str, token: int) -> None:
        try:
            barrier.wait(timeout=10)
            for step in range(200):
                feed.push_simulated_tick(_tick(symbol, token, price=100.0 + step / 100))
        except Exception as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(400):
                snapshot = feed.bootstrap_data_for("NSE:TEST")
                assert snapshot == [{"ts": 1}]
        except Exception as exc:
            errors.append(exc)

    threads = [
        *(
            threading.Thread(target=ticker, args=(symbol, 1000 + i))
            for i, symbol in enumerate(symbols)
        ),
        *(threading.Thread(target=reader) for _ in range(2)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(feed._last_push) == sorted(symbols)
