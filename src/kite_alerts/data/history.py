"""Historical candles, normalisation and the frame merge.

Everything the prototype did implicitly is explicit here:

* the request window is IST-aware and ends at the last **completed** minute, so the
  still-forming candle never enters an indicator;
* rows come back naive and are normalised to an IST-aware index exactly once;
* retries use the shared :class:`RetryPolicy` (capped, jittered, never retrying an
  auth or fatal error);
* one shared :class:`TokenBucket` keeps us inside Kite's ~3 requests/second
  historical limit across every symbol and thread;
* a :class:`CircuitBreaker` stops a failing dependency from being hammered;
* an instrument that rejects ``oi=True`` is retried once without OI instead of
  losing the symbol (``InputException`` used to abort the whole startup).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

import pandas as pd

from ..core.clock import Clock, SystemClock, floor_minute, to_ist_ts
from ..core.errors import DataUnavailableError
from ..core.ports import MetricsPort, metric
from ..core.resilience import CircuitBreaker, RetryPolicy, TokenBucket, retry_call

log = logging.getLogger(__name__)

#: Canonical column order of every OHLC frame in the system.
HISTORY_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "oi")

_INTERVAL = "minute"
#: Kite raises ``InputException`` for an instrument that has no open interest.
_OI_REJECTION_NAMES = frozenset({"InputException"})


def _empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame({column: pd.Series(dtype="float64") for column in HISTORY_COLUMNS})
    frame.index = pd.DatetimeIndex([], name="time", tz="Asia/Kolkata")
    return frame


def normalise_history(rows: Iterable[Mapping[str, Any]] | pd.DataFrame | None) -> pd.DataFrame:
    """Turn raw Kite history rows into a clean, IST-indexed OHLC frame.

    ``volume`` is coerced to ``int64`` and a missing ``oi`` becomes ``0.0``, which
    is what makes the OI rule unavailable rather than wrong for equities.
    """
    if rows is None:
        return _empty_frame()
    records = rows.to_dict("records") if isinstance(rows, pd.DataFrame) else list(rows)
    if not records:
        return _empty_frame()
    first = records[0]
    if not isinstance(first, Mapping):
        raise DataUnavailableError("historical data rows must be mappings")
    if "date" not in first:
        raise DataUnavailableError("historical data is missing the 'date' column")
    for column in ("open", "high", "low", "close"):
        if column not in first:
            raise DataUnavailableError(f"historical data is missing the {column!r} column")

    frame = pd.DataFrame.from_records(records)
    frame.index = pd.DatetimeIndex([to_ist_ts(value) for value in frame["date"]], name="time")

    out = pd.DataFrame(index=frame.index)
    for column in ("open", "high", "low", "close"):
        out[column] = pd.to_numeric(frame[column], errors="coerce")
    out["volume"] = (
        pd.to_numeric(frame["volume"], errors="coerce")
        if "volume" in frame.columns
        else pd.Series(0.0, index=frame.index)
    )
    out["oi"] = (
        pd.to_numeric(frame["oi"], errors="coerce")
        if "oi" in frame.columns
        else pd.Series(0.0, index=frame.index)
    )

    out = out[~out.index.duplicated(keep="last")].sort_index()
    out["volume"] = out["volume"].fillna(0).astype("int64")
    out["oi"] = out["oi"].fillna(0.0).astype(float)
    return out[list(HISTORY_COLUMNS)]


def merge_frames(old: pd.DataFrame | None, new: pd.DataFrame | None, max_bars: int) -> pd.DataFrame:
    """Union of ``old`` and ``new``, newest last, capped to ``max_bars`` bars.

    Overlapping timestamps keep the *newer* row, which is what makes a reconnect
    back-fill able to correct a candle that was built from a partial tick stream.
    """
    if max_bars < 1:
        raise ValueError("max_bars must be >= 1")
    if old is None or old.empty:
        merged = new if new is not None else _empty_frame()
    elif new is None or new.empty:
        merged = old
    else:
        merged = pd.concat([old, new])
    if merged.empty:
        return merged
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    return merged.tail(max_bars)


class HistoryClient:
    """Rate-limited, retrying, breaker-protected access to ``historical_data``."""

    def __init__(
        self,
        kite: Any,
        *,
        settings: Any = None,
        metrics: MetricsPort | None = None,
        clock: Clock | None = None,
        sleep: Any = time.sleep,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        from ..config.settings import ResilienceSettings

        self._kite = kite
        self._settings = settings if settings is not None else ResilienceSettings()
        self._metrics = metrics
        self._clock: Clock = clock or SystemClock()
        self._sleep = sleep
        self._policy = RetryPolicy(
            retries=self._settings.history_max_retries,
            base_seconds=self._settings.retry_base_seconds,
            cap_seconds=self._settings.retry_cap_seconds,
        )
        self._bucket = TokenBucket(self._settings.history_rate_limit_per_sec, sleep=sleep)
        self._breaker = breaker or CircuitBreaker(
            failure_threshold=self._settings.circuit_failure_threshold,
            reset_seconds=self._settings.circuit_reset_seconds,
            name="kite.historical_data",
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def fetch(self, token: int, *, days: int = 1, continuous: bool = False) -> pd.DataFrame:
        """Fetch ``days`` of one-minute candles for ``token``.

        The window ends at the start of the current minute, so the candle still
        forming is excluded by construction rather than by a race.
        """
        if days < 1:
            raise ValueError("days must be >= 1")
        now = to_ist_ts(self._clock.now())
        to_date = floor_minute(now)
        from_date = now - pd.Timedelta(days=int(days))
        rows = self._breaker.call(self._request, token, from_date, to_date, continuous)
        frame = normalise_history(rows)
        if frame.empty:
            return frame
        frame = frame[frame.index < to_date]
        metric(self._metrics, "history_bars", float(len(frame)), token=str(token))
        return frame

    # ------------------------------------------------------------------ internals
    def _request(
        self,
        token: int,
        from_date: pd.Timestamp,
        to_date: pd.Timestamp,
        continuous: bool,
    ) -> Sequence[Mapping[str, Any]]:
        self._bucket.acquire()
        return retry_call(
            self._rows,
            token,
            from_date,
            to_date,
            continuous,
            policy=self._policy,
            sleep=self._sleep,
            on_retry=self._on_retry,
        )

    def _rows(
        self,
        token: int,
        from_date: pd.Timestamp,
        to_date: pd.Timestamp,
        continuous: bool,
    ) -> Sequence[Mapping[str, Any]]:
        try:
            return cast(
                "Sequence[Mapping[str, Any]]",
                self._kite.historical_data(
                    token, from_date, to_date, _INTERVAL, continuous=continuous, oi=True
                ),
            )
        except Exception as exc:
            if type(exc).__name__ not in _OI_REJECTION_NAMES:
                raise
            metric(self._metrics, "history_oi_unsupported", 1, token=str(token))
            log.info("token %s rejected oi=True (%s); retrying without open interest", token, exc)
            return cast(
                "Sequence[Mapping[str, Any]]",
                self._kite.historical_data(
                    token, from_date, to_date, _INTERVAL, continuous=continuous, oi=False
                ),
            )

    def _on_retry(self, attempt: int, exc: BaseException, delay: float, kind: str) -> None:
        metric(self._metrics, "history_retries", 1, kind=kind)
        log.warning(
            "historical_data retry %d in %.2fs after %s: %s",
            attempt + 1,
            delay,
            type(exc).__name__,
            exc,
        )
