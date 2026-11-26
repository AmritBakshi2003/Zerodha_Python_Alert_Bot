"""F21: watchlist writes must not race.

Two concurrent "add" requests read the whole file, append their symbol and write
it back. Without a lock the last writer wins (a symbol disappears), and with a
shared temp name two writers can overwrite each other mid-write.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from kite_alerts.ui.chart.service import WATCHLIST_LIMIT
from unit.ui_support_c3 import make_chart_context


def test_temp_file_name_is_unique_per_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each writer gets its own temp file, so two writes cannot collide (F21)."""
    context, _fake = make_chart_context(tmp_path)
    chart = context.chart

    seen: list[str] = []
    real_replace = Path.replace

    def recording_replace(self: Path, target: Path) -> Path:
        seen.append(self.name)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", recording_replace)

    chart.add_watchlist("nse_delayed", "NSE:INFY")
    chart.add_watchlist("nse_delayed", "NSE:TCS")

    assert len(seen) == 2
    assert seen[0] != seen[1], f"both writers used the same temp file: {seen}"


def test_concurrent_adds_keep_every_symbol(tmp_path: Path) -> None:
    """WATCHLIST_LIMIT concurrent adds must all survive (F21 acceptance).

    The plan said 20 threads, but the product caps the user-added list at
    ``WATCHLIST_LIMIT`` (10), so 10 is the largest correct burst.
    """
    context, _fake = make_chart_context(tmp_path)
    chart = context.chart
    symbols = [f"NSE:RACE{i}" for i in range(WATCHLIST_LIMIT)]
    barrier = threading.Barrier(len(symbols))
    errors: list[BaseException] = []

    def add(symbol: str) -> None:
        try:
            barrier.wait(timeout=10)
            chart.add_watchlist("nse_delayed", symbol)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(symbol,)) for symbol in symbols]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)
    path = tmp_path / "data" / "chart_watchlist.json"
    stored = json.loads(path.read_text(encoding="utf-8"))["symbols"]["nse_delayed"]
    assert sorted(stored) == sorted(symbols)
    assert not list((tmp_path / "data").glob("*.tmp"))
