"""Instrument resolution: aliases, cache, expiry warnings, per-symbol errors."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from fakes.fake_kite import FakeInstrument, FakeKite
from kite_alerts.core.clock import FrozenClock
from kite_alerts.core.errors import ConfigError, DataUnavailableError
from kite_alerts.data.instruments import Instrument, InstrumentResolver

TODAY = dt.date(2026, 11, 16)


def kite() -> FakeKite:
    return FakeKite(
        instruments=[
            FakeInstrument(111, "NSE", "TEST", name="TEST"),
            FakeInstrument(
                222,
                "NFO",
                "NIFTY26NOVFUT",
                name="NIFTY",
                instrument_type="FUT",
                expiry=dt.date(2026, 11, 26),
            ),
            FakeInstrument(
                333,
                "NFO",
                "NIFTY26DECFUT",
                name="NIFTY",
                instrument_type="FUT",
                expiry=dt.date(2026, 12, 31),
            ),
            FakeInstrument(
                444,
                "NFO",
                "NIFTY26OCTFUT",
                name="NIFTY",
                instrument_type="FUT",
                expiry=dt.date(2026, 10, 30),
            ),
            FakeInstrument(
                555,
                "NFO",
                "BANKNIFTY26NOVFUT",
                name="BANKNIFTY",
                instrument_type="FUT",
                expiry=dt.date(2026, 11, 26),
            ),
        ]
    )


def resolver(tmp_path: Path, **kwargs) -> InstrumentResolver:
    return InstrumentResolver(
        kite(), tmp_path / "cache", clock=FrozenClock("2026-11-16 09:00:00"), **kwargs
    )


class TestResolve:
    def test_exact_equity(self, tmp_path: Path) -> None:
        instrument = resolver(tmp_path).resolve("NSE:TEST")
        assert instrument.token == 111
        assert instrument.symbol == "NSE:TEST"

    def test_case_insensitive(self, tmp_path: Path) -> None:
        assert resolver(tmp_path).resolve("nse:test").token == 111

    def test_explicit_future(self, tmp_path: Path) -> None:
        instrument = resolver(tmp_path).resolve("NFO:NIFTY26DECFUT")
        assert instrument.token == 333

    def test_nearest_expiry_alias(self, tmp_path: Path) -> None:
        target = resolver(tmp_path)
        instrument = target.resolve("NFO:NIFTY-FUT")
        assert instrument.token == 222  # Nov expiry, not Dec, not the expired Oct
        assert target.aliases["NFO:NIFTY-FUT"] == "NFO:NIFTY26NOVFUT"

    def test_alias_skips_expired_contracts(self, tmp_path: Path) -> None:
        instrument = resolver(tmp_path).resolve("NFO:NIFTY-FUT")
        assert instrument.expiry is not None
        assert instrument.expiry >= TODAY

    def test_unknown_symbol_suggests_alternatives(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="did you mean"):
            resolver(tmp_path).resolve("NSE:TESTING")

    def test_unknown_alias(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no unexpired"):
            resolver(tmp_path).resolve("NFO:FINNIFTY-FUT")

    def test_missing_exchange_prefix(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="EXCHANGE"):
            resolver(tmp_path).resolve("TEST")

    def test_unknown_exchange(self, tmp_path: Path) -> None:
        with pytest.raises(DataUnavailableError):
            resolver(tmp_path).resolve("BSE:TEST")

    def test_resolve_many_collects_errors(self, tmp_path: Path) -> None:
        resolved, errors = resolver(tmp_path).resolve_many(
            ["NSE:TEST", "NSE:NOPE", "NFO:NIFTY-FUT"]
        )
        assert set(resolved) == {"NSE:TEST", "NFO:NIFTY-FUT"}
        assert set(errors) == {"NSE:NOPE"}


class TestCache:
    def test_second_resolver_reads_the_cache(self, tmp_path: Path) -> None:
        fake = kite()
        first = InstrumentResolver(
            fake,
            tmp_path / "cache",
            clock=FrozenClock("2026-11-16 09:00:00"),
            ttl_hours=24 * 365,
        )
        first.resolve("NSE:TEST")
        calls_after_first = len(fake.instrument_calls)

        second = InstrumentResolver(
            fake,
            tmp_path / "cache",
            clock=FrozenClock("2026-11-16 10:00:00"),
            ttl_hours=24 * 365,
        )
        second.resolve("NSE:TEST")
        assert len(fake.instrument_calls) == calls_after_first
        assert (tmp_path / "cache" / "instruments_NSE.csv.gz").exists()

    def test_stale_cache_is_refreshed(self, tmp_path: Path) -> None:
        fake = kite()
        first = InstrumentResolver(
            fake, tmp_path / "cache", clock=FrozenClock("2026-11-01 09:00:00"), ttl_hours=1
        )
        first.resolve("NSE:TEST")
        calls = len(fake.instrument_calls)
        # A clock far in the future makes the cached file stale.
        later = InstrumentResolver(
            fake, tmp_path / "cache", clock=FrozenClock("2026-12-01 09:00:00"), ttl_hours=1
        )
        later.resolve("NSE:TEST")
        assert len(fake.instrument_calls) > calls


class TestExpiry:
    def test_warning_within_two_days(self) -> None:
        instrument = Instrument(
            token=1,
            exchange="NFO",
            tradingsymbol="NIFTY26NOVFUT",
            instrument_type="FUT",
            expiry=dt.date(2026, 11, 17),
        )
        warning = instrument.expiry_warning(TODAY)
        assert warning is not None
        assert "expires in 1 day" in warning

    def test_expired_instrument_warns(self) -> None:
        instrument = Instrument(
            token=1,
            exchange="NFO",
            tradingsymbol="OLD",
            instrument_type="FUT",
            expiry=dt.date(2026, 11, 1),
        )
        assert "expired" in (instrument.expiry_warning(TODAY) or "")

    def test_no_warning_for_distant_or_missing_expiry(self) -> None:
        far = Instrument(token=1, exchange="NFO", tradingsymbol="X", expiry=dt.date(2027, 1, 1))
        equity = Instrument(token=2, exchange="NSE", tradingsymbol="TEST")
        assert far.expiry_warning(TODAY) is None
        assert equity.expiry_warning(TODAY) is None

    def test_resolver_reports_warnings_for_the_set(self, tmp_path: Path) -> None:
        target = resolver(tmp_path)
        near = Instrument(
            token=9,
            exchange="NFO",
            tradingsymbol="NIFTY26NOVFUT",
            instrument_type="FUT",
            expiry=dt.date(2026, 11, 17),
        )
        warnings = target.expiry_warnings([near])
        assert any("expires in 1 day" in warning for warning in warnings)
