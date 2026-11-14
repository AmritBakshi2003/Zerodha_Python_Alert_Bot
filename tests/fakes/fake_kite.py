"""FakeKite / FakeTicker: the whole live path without a Kite session.

The live pipeline had never been exercised against a real session, so these
doubles model the SDK's *shape* precisely:

* ``historical_data(token, from_date, to_date, interval, continuous, oi)`` returns
  naive-IST dict rows, exactly like the real client;
* ``instruments(exchange)`` returns the dump rows the resolver parses;
* ``KiteTicker`` fires ``on_connect`` on ``connect()`` and exposes
  ``subscribe``/``set_mode``/``stop``/``close`` plus the reconnect callbacks;
* exceptions are named like the real ones (``TokenException``, ``InputException``)
  because error classification is name-based, as the SDK requires.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pandas as pd


class TokenException(Exception):
    """Same name as ``kiteconnect.exceptions.TokenException``."""


class InputException(Exception):
    """Same name as ``kiteconnect.exceptions.InputException``."""


class NetworkException(Exception):
    """Same name as ``kiteconnect.exceptions.NetworkException``."""


@dataclass(frozen=True)
class FakeInstrument:
    token: int
    exchange: str
    tradingsymbol: str
    name: str = ""
    segment: str = "NSE"
    instrument_type: str = "EQ"
    lot_size: int = 1
    tick_size: float = 0.05
    expiry: dt.date | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "instrument_token": self.token,
            "exchange": self.exchange,
            "tradingsymbol": self.tradingsymbol,
            "name": self.name,
            "segment": self.segment,
            "instrument_type": self.instrument_type,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
            "expiry": self.expiry.isoformat() if self.expiry else "",
        }


def instrument_rows(instruments: Iterable[FakeInstrument]) -> list[dict[str, Any]]:
    return [instrument.as_row() for instrument in instruments]


class FakeKite:
    """Minimal read-only KiteConnect replacement."""

    def __init__(
        self,
        *,
        instruments: Sequence[FakeInstrument] | None = None,
        history: dict[int, pd.DataFrame] | None = None,
        profile_error: Exception | None = None,
        oi_unsupported: bool = False,
    ) -> None:
        self.instruments_data = list(instruments or [])
        self.history_data = dict(history or {})
        self.profile_error = profile_error
        self.oi_unsupported = oi_unsupported
        self.access_token: str | None = None
        self.history_calls: list[dict[str, Any]] = []
        self.history_error: Exception | None = None
        self.instrument_calls: list[str | None] = []

    # ------------------------------------------------------------------ session
    def set_access_token(self, token: str) -> None:
        self.access_token = token

    def profile(self) -> dict[str, Any]:
        if self.profile_error is not None:
            raise self.profile_error
        return {"user_id": "FAKE", "user_name": "Fake Trader"}

    # ------------------------------------------------------------------ metadata
    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        self.instrument_calls.append(exchange)
        rows = [row.as_row() for row in self.instruments_data]
        if exchange is None:
            return rows
        return [row for row in rows if row["exchange"] == exchange.upper()]

    # ------------------------------------------------------------------ history
    def historical_data(
        self,
        instrument_token: int,
        from_date: dt.datetime,
        to_date: dt.datetime,
        interval: str,
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict[str, Any]]:
        self.history_calls.append(
            {
                "token": instrument_token,
                "from_date": from_date,
                "to_date": to_date,
                "interval": interval,
                "continuous": continuous,
                "oi": oi,
            }
        )
        if self.history_error is not None:
            raise self.history_error
        if oi and self.oi_unsupported:
            raise InputException("oi is not supported for this instrument")

        frame = self.history_data.get(instrument_token)
        if frame is None or frame.empty:
            return []

        start = pd.Timestamp(from_date)
        end = pd.Timestamp(to_date)
        start = start.tz_localize("Asia/Kolkata") if start.tz is None else start
        end = end.tz_localize("Asia/Kolkata") if end.tz is None else end

        rows: list[dict[str, Any]] = []
        for ts, row in frame.iterrows():
            moment = pd.Timestamp(ts)
            if moment.tz is None:
                moment = moment.tz_localize("Asia/Kolkata")
            if not (start <= moment <= end):
                continue
            rows.append(
                {
                    # Real Kite returns naive IST datetimes.
                    "date": moment.tz_localize(None).to_pydatetime(),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row.get("volume", 0)),
                    "oi": float(row.get("oi", 0.0)) if oi else 0.0,
                }
            )
        return rows


class FakeTicker:
    """Stand-in for ``kiteconnect.KiteTicker`` with manual tick injection."""

    MODE_LTP = "ltp"
    MODE_QUOTE = "quote"
    MODE_FULL = "full"

    instances: ClassVar[list[FakeTicker]] = []

    def __init__(self, api_key: str, access_token: str, **kwargs: Any) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.kwargs = kwargs
        self.connected = False
        self.stopped = False
        self.closed = False
        self.subscribed: list[int] = []
        self.modes: list[tuple[str, list[int]]] = []
        self.connect_args: tuple[Any, ...] = ()
        self.on_ticks: Any = None
        self.on_connect: Any = None
        self.on_close: Any = None
        self.on_error: Any = None
        self.on_reconnect: Any = None
        self.on_noreconnect: Any = None
        FakeTicker.instances.append(self)

    # ------------------------------------------------------------------ SDK surface
    def connect(self, threaded: bool = False, **_: Any) -> None:
        self.connect_args = (threaded,)
        self.connected = True
        self.stopped = False
        self.closed = False
        if self.on_connect is not None:
            self.on_connect(self, {"type": "connect"})

    def subscribe(self, tokens: Sequence[int]) -> None:
        self.subscribed.extend(int(token) for token in tokens)

    def set_mode(self, mode: str, tokens: Sequence[int]) -> None:
        self.modes.append((mode, [int(token) for token in tokens]))

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------ test controls
    def push(self, ticks: Sequence[dict[str, Any]]) -> None:
        assert self.on_ticks is not None, "feed did not register on_ticks"
        self.on_ticks(self, list(ticks))

    def drop(self, code: int = 1006, reason: str = "connection closed") -> None:
        assert self.on_close is not None
        self.on_close(self, code, reason)

    def error(self, code: int = 0, reason: str = "") -> None:
        assert self.on_error is not None
        self.on_error(self, code, reason)

    def reconnect(self, attempts: int = 1) -> None:
        if self.on_reconnect is not None:
            self.on_reconnect(self, attempts)

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()


@dataclass
class RecordingNotifier:
    """Captures messages instead of delivering them."""

    name: str = "recording"
    messages: list[Any] = field(default_factory=list)
    closed: bool = False
    fail_times: int = 0
    error: Exception | None = None
    attempts: int = 0

    def send(self, message: Any) -> None:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            from kite_alerts.notifiers.base import DeliveryError

            raise DeliveryError(self.name, "simulated failure")
        self.messages.append(message)

    def close(self) -> None:
        self.closed = True

    @property
    def texts(self) -> list[str]:
        return [message.text for message in self.messages]
