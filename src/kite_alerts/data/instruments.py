"""Instrument resolution from the Kite instrument dump.

``kite.instruments(exchange)`` is a heavy call (tens of thousands of rows), so the
dump is downloaded per exchange, cached on disk for a day, and reused. Only the
exchange actually needed is downloaded.

Two conveniences the prototype lacked, both of which matter at expiry:

* ``NFO:NIFTY-FUT`` is an *alias* for the nearest-expiry **unexpired** futures
  contract, resolved from the dump and remembered in :attr:`aliases` so that a
  per-symbol override written against the alias survives a contract roll;
* an instrument close to (or past) expiry produces an explicit warning at startup
  instead of an unexplained silent symbol on expiry day.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from ..core.clock import Clock, SystemClock, to_ist, to_ist_ts
from ..core.errors import ConfigError, DataUnavailableError, KiteAlertsError
from ..core.ports import MetricsPort, metric

log = logging.getLogger(__name__)

#: Warn when a contract expires within this many calendar days.
EXPIRY_WARNING_DAYS = 2

_ALIAS_SUFFIX = "-FUT"
_CSV_TEMPLATE = "instruments_{exchange}.csv.gz"
_META_TEMPLATE = "instruments_{exchange}.meta.json"


@dataclass(frozen=True, slots=True)
class Instrument:
    """One row of the Kite instrument dump, reduced to what we need."""

    token: int
    exchange: str
    tradingsymbol: str
    name: str = ""
    segment: str = ""
    instrument_type: str = ""
    lot_size: int = 1
    tick_size: float = 0.05
    expiry: dt.date | None = None

    @property
    def symbol(self) -> str:
        """``EXCHANGE:TRADINGSYMBOL`` - the canonical identifier used everywhere."""
        return f"{self.exchange}:{self.tradingsymbol}"

    @property
    def is_future(self) -> bool:
        return self.instrument_type.upper().startswith("FUT")

    def expiry_warning(self, today: dt.date) -> str | None:
        """Warning for a contract that has expired or is about to, else ``None``."""
        if self.expiry is None:
            return None
        days = (self.expiry - today).days
        if days < 0:
            return (
                f"{self.symbol} expired on {self.expiry.isoformat()}; "
                "re-resolve the contract or use the NFO:<UNDERLYING>-FUT alias"
            )
        if days <= EXPIRY_WARNING_DAYS:
            unit = "day" if days == 1 else "days"
            return (
                f"{self.symbol} expires in {days} {unit} "
                f"({self.expiry.isoformat()}); it rolls to the next contract after that"
            )
        return None


class InstrumentResolver:
    """Resolve ``EXCHANGE:TRADINGSYMBOL`` specs, with an on-disk dump cache."""

    def __init__(
        self,
        kite: Any,
        cache_dir: str | Path,
        *,
        clock: Clock | None = None,
        metrics: MetricsPort | None = None,
        ttl_hours: float = 24.0,
    ) -> None:
        self._kite = kite
        self._cache_dir = Path(cache_dir)
        self._clock: Clock = clock or SystemClock()
        self._metrics = metrics
        self._ttl_seconds = float(ttl_hours) * 3600.0
        self._dumps: dict[str, list[Instrument]] = {}
        self._aliases: dict[str, str] = {}

    # ------------------------------------------------------------------ public
    @property
    def aliases(self) -> dict[str, str]:
        """Configured alias -> resolved symbol, e.g. ``NFO:NIFTY-FUT`` -> ``NFO:NIFTY26NOVFUT``."""
        return dict(self._aliases)

    @property
    def today(self) -> dt.date:
        return to_ist(self._clock.now()).date()

    def resolve(self, spec: str) -> Instrument:
        """Resolve one spec, or raise a ``ConfigError`` explaining why not."""
        exchange, symbol = _split_spec(spec)
        dump = self._dump(exchange)
        if symbol.endswith(_ALIAS_SUFFIX):
            underlying = symbol[: -len(_ALIAS_SUFFIX)]
            instrument = self._nearest_future(dump, underlying, spec)
            self._aliases[spec.strip().upper()] = instrument.symbol
            return instrument
        for instrument in dump:
            if instrument.tradingsymbol.upper() == symbol:
                return instrument
        raise ConfigError(self._unknown_message(spec, exchange, symbol, dump))

    def resolve_many(self, specs: Iterable[str]) -> tuple[dict[str, Instrument], dict[str, str]]:
        """Resolve every spec, collecting per-spec errors instead of aborting.

        Returns ``(resolved keyed by the spec as configured, errors)`` so the
        alias a user wrote is what appears in configuration and alerts.
        """
        resolved: dict[str, Instrument] = {}
        errors: dict[str, str] = {}
        for spec in specs:
            try:
                resolved[spec] = self.resolve(spec)
            except KiteAlertsError as exc:
                errors[spec] = str(exc)
            except Exception as exc:  # defensive: one bad symbol must not abort the rest
                errors[spec] = f"{type(exc).__name__}: {exc}"
        return resolved, errors

    def expiry_warnings(self, instruments: Iterable[Instrument]) -> list[str]:
        today = self.today
        return [
            warning
            for warning in (instrument.expiry_warning(today) for instrument in instruments)
            if warning
        ]

    # ------------------------------------------------------------------ resolution
    def _nearest_future(self, dump: list[Instrument], underlying: str, spec: str) -> Instrument:
        today = self.today
        candidates = [
            instrument
            for instrument in dump
            if instrument.is_future
            and instrument.expiry is not None
            and instrument.expiry >= today
            and instrument.name.strip().upper() == underlying
        ]
        if not candidates:
            raise ConfigError(
                f"no unexpired {underlying} futures contract in the instrument dump "
                f"(requested {spec!r}); check the underlying name or list the exact "
                "tradingsymbol"
            )
        return min(candidates, key=lambda instrument: instrument.expiry or dt.date.max)

    def _unknown_message(
        self, spec: str, exchange: str, symbol: str, dump: list[Instrument]
    ) -> str:
        names = [instrument.tradingsymbol for instrument in dump]
        suggestions = difflib.get_close_matches(symbol, names, n=3, cutoff=0.6)
        if suggestions:
            return (
                f"{spec!r} is not in the {exchange} instrument dump; "
                f"did you mean: {', '.join(suggestions)}?"
            )
        return (
            f"{spec!r} is not in the {exchange} instrument dump "
            f"({len(names)} instrument(s) checked); check the exchange and the "
            "tradingsymbol, or use the NFO:<UNDERLYING>-FUT alias"
        )

    # ------------------------------------------------------------------ dump cache
    def _dump(self, exchange: str) -> list[Instrument]:
        cached = self._dumps.get(exchange)
        if cached is not None:
            return cached
        instruments = self._read_cache(exchange)
        if instruments is None:
            instruments = self._download(exchange)
            self._write_cache(exchange, instruments)
        self._dumps[exchange] = instruments
        return instruments

    def _download(self, exchange: str) -> list[Instrument]:
        metric(self._metrics, "instrument_downloads", 1, exchange=exchange)
        try:
            rows = self._kite.instruments(exchange)
        except Exception as exc:
            raise DataUnavailableError(
                f"could not download the {exchange} instrument dump: {exc}"
            ) from exc
        instruments = [
            instrument for instrument in (_from_row(row) for row in rows or ()) if instrument
        ]
        if not instruments:
            raise DataUnavailableError(
                f"the {exchange} instrument dump is empty; check the exchange name and the "
                "Kite instruments entitlement"
            )
        return instruments

    def _paths(self, exchange: str) -> tuple[Path, Path]:
        return (
            self._cache_dir / _CSV_TEMPLATE.format(exchange=exchange),
            self._cache_dir / _META_TEMPLATE.format(exchange=exchange),
        )

    def _read_cache(self, exchange: str) -> list[Instrument] | None:
        csv_path, meta_path = self._paths(exchange)
        if not csv_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = to_ist_ts(meta["fetched_at"])
            age = (to_ist_ts(self._clock.now()) - fetched_at).total_seconds()
            if age > self._ttl_seconds:
                metric(self._metrics, "instrument_cache_stale", 1, exchange=exchange)
                return None
            frame = pd.read_csv(csv_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("ignoring unreadable instrument cache %s: %s", csv_path, exc)
            return None
        instruments = [
            instrument
            for instrument in (_from_row(row) for row in frame.to_dict("records"))
            if instrument
        ]
        if not instruments:
            return None
        metric(self._metrics, "instrument_cache_hits", 1, exchange=exchange)
        return instruments

    def _write_cache(self, exchange: str, instruments: list[Instrument]) -> None:
        csv_path, meta_path = self._paths(exchange)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            frame = pd.DataFrame([_to_row(instrument) for instrument in instruments])
            frame.to_csv(csv_path, index=False, compression="gzip")
            meta_path.write_text(
                json.dumps(
                    {
                        "exchange": exchange,
                        "fetched_at": to_ist_ts(self._clock.now()).isoformat(),
                        "count": len(instruments),
                    }
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            # A cache write must never stop the bot from starting.
            log.warning("could not cache the %s instrument dump: %s", exchange, exc)
            metric(self._metrics, "instrument_cache_write_errors", 1, exchange=exchange)


def _split_spec(spec: str) -> tuple[str, str]:
    text = (spec or "").strip()
    if ":" not in text:
        raise ConfigError(
            f"SYMBOLS entry {spec!r} must be EXCHANGE:TRADINGSYMBOL "
            "(e.g. NSE:RELIANCE, NFO:NIFTY26NOVFUT or the alias NFO:NIFTY-FUT)"
        )
    exchange, symbol = text.split(":", 1)
    exchange, symbol = exchange.strip().upper(), symbol.strip().upper()
    if not exchange or not symbol:
        raise ConfigError(f"SYMBOLS entry {spec!r} must be EXCHANGE:TRADINGSYMBOL")
    return exchange, symbol


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _from_row(row: Mapping[str, Any]) -> Instrument | None:
    token = _as_int(row.get("instrument_token"), 0)
    tradingsymbol = str(row.get("tradingsymbol") or "").strip().upper()
    if token <= 0 or not tradingsymbol:
        return None
    expiry: dt.date | None = None
    raw_expiry = row.get("expiry")
    if raw_expiry is not None and str(raw_expiry).strip() not in ("", "nan", "NaT", "None"):
        try:
            expiry = pd.Timestamp(str(raw_expiry)).date()
        except (TypeError, ValueError):
            expiry = None
    return Instrument(
        token=token,
        exchange=str(row.get("exchange") or "").strip().upper(),
        tradingsymbol=tradingsymbol,
        name=str(row.get("name") or "").strip(),
        segment=str(row.get("segment") or "").strip(),
        instrument_type=str(row.get("instrument_type") or "").strip().upper(),
        lot_size=_as_int(row.get("lot_size"), 1),
        tick_size=_as_float(row.get("tick_size"), 0.0),
        expiry=expiry,
    )


def _to_row(instrument: Instrument) -> dict[str, Any]:
    return {
        "instrument_token": instrument.token,
        "exchange": instrument.exchange,
        "tradingsymbol": instrument.tradingsymbol,
        "name": instrument.name,
        "segment": instrument.segment,
        "instrument_type": instrument.instrument_type,
        "lot_size": instrument.lot_size,
        "tick_size": instrument.tick_size,
        "expiry": "" if instrument.expiry is None else instrument.expiry.isoformat(),
    }
