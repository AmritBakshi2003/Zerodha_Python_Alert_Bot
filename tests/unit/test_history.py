"""Historical data: IST windows, normalisation, retries, breaker, OI fallback."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest import make_frame
from fakes.fake_kite import FakeKite, NetworkException
from kite_alerts.config.settings import ResilienceSettings
from kite_alerts.core.clock import FrozenClock
from kite_alerts.core.errors import CircuitOpenError, DataUnavailableError
from kite_alerts.core.resilience import CircuitBreaker
from kite_alerts.data.history import HistoryClient, normalise_history
from kite_alerts.observability.metrics import Metrics


def rows_for(frame: pd.DataFrame) -> list[dict[str, object]]:
    return [
        {
            "date": ts.tz_localize(None).to_pydatetime(),
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": int(row["volume"]),
            "oi": float(row["oi"]),
        }
        for ts, row in frame.iterrows()
    ]


class TestNormalisation:
    def test_naive_rows_become_ist(self) -> None:
        frame = make_frame(np.linspace(100, 110, 10))
        result = normalise_history(rows_for(frame))
        assert str(result.index.tz) in ("Asia/Kolkata", "IST")
        assert result.index[0].strftime("%H:%M") == "09:15"
        assert list(result.columns) == ["open", "high", "low", "close", "volume", "oi"]

    def test_duplicates_keep_the_last_row(self) -> None:
        rows = rows_for(make_frame([100.0, 101.0]))
        rows.append({**rows[-1], "close": 999.0})
        result = normalise_history(rows)
        assert result["close"].iloc[-1] == 999.0

    def test_missing_oi_defaults_to_zero(self) -> None:
        rows = rows_for(make_frame([100.0, 101.0]))
        for row in rows:
            row.pop("oi")
        assert normalise_history(rows)["oi"].tolist() == [0.0, 0.0]

    def test_empty_inputs(self) -> None:
        assert normalise_history(None).empty
        assert normalise_history([]).empty

    def test_missing_date_column_raises(self) -> None:
        with pytest.raises(DataUnavailableError, match="date"):
            normalise_history([{"open": 1, "high": 1, "low": 1, "close": 1}])

    def test_missing_close_column_raises(self) -> None:
        with pytest.raises(DataUnavailableError, match="close"):
            normalise_history([{"date": "2026-11-16 09:15", "open": 1, "high": 1, "low": 1}])


class TestFetch:
    def client(self, fake: FakeKite, clock: FrozenClock, **kwargs) -> HistoryClient:
        settings = ResilienceSettings(history_max_retries=1, retry_base_seconds=0.01)
        return HistoryClient(
            fake, settings=settings, clock=clock, metrics=Metrics(), sleep=lambda _s: None, **kwargs
        )

    def test_window_is_ist_and_forming_minute_is_dropped(self) -> None:
        clock = FrozenClock("2026-11-16 09:20:30")
        frame = make_frame(np.linspace(100, 110, 10))  # 09:15..09:24
        fake = FakeKite(history={111: frame})
        result = self.client(fake, clock).fetch(111, days=1)

        call = fake.history_calls[0]
        assert call["from_date"].utcoffset().total_seconds() == 5.5 * 3600
        assert call["to_date"].strftime("%H:%M") == "09:20"
        assert call["interval"] == "minute"
        # 09:20 is still forming at 09:20:30 -> dropped; 09:15..09:19 remain.
        assert result.index[-1].strftime("%H:%M") == "09:19"
        assert len(result) == 5

    def test_empty_history_is_returned_empty(self) -> None:
        clock = FrozenClock("2026-11-16 10:00:00")
        result = self.client(FakeKite(), clock).fetch(999, days=1)
        assert result.empty

    def test_oi_rejection_falls_back_without_oi(self) -> None:
        clock = FrozenClock("2026-11-16 10:00:00")
        frame = make_frame(np.linspace(100, 110, 10))
        fake = FakeKite(history={111: frame}, oi_unsupported=True)
        result = self.client(fake, clock).fetch(111, days=1)
        assert not result.empty
        assert [call["oi"] for call in fake.history_calls] == [True, False]

    def test_network_failure_raises_and_opens_the_breaker(self) -> None:
        clock = FrozenClock("2026-11-16 10:00:00")
        fake = FakeKite()
        fake.history_error = NetworkException("connection reset")
        breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60)
        client = self.client(fake, clock, breaker=breaker)

        for _ in range(2):
            with pytest.raises(NetworkException):
                client.fetch(111, days=1)
        assert breaker.state == "open"
        with pytest.raises(CircuitOpenError):
            client.fetch(111, days=1)

    def test_retries_a_transient_error(self) -> None:
        clock = FrozenClock("2026-11-16 10:00:00")
        frame = make_frame(np.linspace(100, 110, 5))
        fake = FakeKite(history={111: frame})
        settings = ResilienceSettings(history_max_retries=3, retry_base_seconds=0.01)
        calls: list[int] = []
        original = fake.historical_data

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise NetworkException("blip")
            return original(*args, **kwargs)

        fake.historical_data = flaky  # type: ignore[assignment]
        client = HistoryClient(fake, settings=settings, clock=clock, sleep=lambda _s: None)
        result = client.fetch(111, days=1)
        assert not result.empty
        assert len(calls) == 2

    def test_invalid_days(self) -> None:
        clock = FrozenClock("2026-11-16 10:00:00")
        with pytest.raises(ValueError):
            self.client(FakeKite(), clock).fetch(111, days=0)

    def test_oi_flag_is_forwarded(self) -> None:
        clock = FrozenClock("2026-11-16 10:00:00")
        fake = FakeKite(history={111: make_frame([100.0, 101.0])})
        self.client(fake, clock).fetch(111, days=1)
        assert fake.history_calls[0]["oi"] is True
        assert fake.history_calls[0]["continuous"] is False
