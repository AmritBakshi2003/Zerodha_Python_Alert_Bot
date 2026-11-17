"""Manual smoke test for the two free chart providers (F16).

CI only ever sees fakes. Run this by hand on a networked machine to prove the
real Yahoo Finance and Kraken OHLC shapes still match what the adapters parse:

    py -3 scripts/smoke_sources.py
    py -3 scripts/smoke_sources.py --provider kraken --interval 5
    py -3 scripts/smoke_sources.py --provider nse --symbol NSE:TCS

Exit code 0 means every requested provider returned usable candles in the
documented shape. Exit code 1 means a provider failed or changed shape. The
message is generic and never includes a provider URL or response body.

This script is deliberately not part of CI: it needs internet access, and it
must never be able to feed the notifier or the alert store. It only calls the
same read-only adapters the dashboard uses, with the same allowlists.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from kite_alerts.ui.chart.sources import (
    INTERVALS,
    CryptoShowcaseSource,
    DelayedNSESource,
    yfinance_history,
)
from kite_alerts.ui.chart.types import SourceError, SourceSeries


@dataclass(frozen=True)
class Probe:
    """What one provider returned, in the terms a human can check."""

    provider: str
    symbol: str
    badge: str
    candles: int
    last_candle_utc: int
    lag_seconds: float | None
    stale: bool
    message: str


class SmokeError(RuntimeError):
    """A provider failed, or its payload no longer has the documented shape."""


def _probe(provider: str, symbol: str, badge: str, series: SourceSeries) -> Probe:
    candles = series.candles
    if not candles:
        raise SmokeError(f"{provider}: provider returned no candles")
    return Probe(
        provider=provider,
        symbol=symbol,
        badge=badge,
        candles=len(candles),
        last_candle_utc=candles[-1].time,
        lag_seconds=series.delay_seconds,
        stale=series.stale,
        message=series.message,
    )


def probe_nse(
    history: Callable[[str, int], pd.DataFrame] = yfinance_history,
    *,
    symbol: str = "NSE:RELIANCE",
    interval: int = 1,
    now: Callable[[], float] = time.time,
) -> Probe:
    """Fetch one real delayed-NSE sample and describe it."""
    source = DelayedNSESource(history=history, now=now)
    try:
        series = source.fetch(symbol, interval, None)
    except SourceError as exc:
        raise SmokeError(f"nse_delayed: {exc.message}") from None
    except ValueError as exc:
        raise SmokeError(f"nse_delayed: response shape changed ({exc})") from None
    return _probe("nse_delayed", symbol, source.badge, series)


def probe_crypto(
    http_get: Callable[..., Any] | None = None,
    *,
    symbol: str = "BTC",
    interval: int = 1,
    now: Callable[[], float] = time.time,
) -> Probe:
    """Fetch one real Kraken sample and describe it."""
    kwargs: dict[str, Any] = {"now": now}
    if http_get is not None:
        kwargs["http_get"] = http_get
    source = CryptoShowcaseSource(**kwargs)
    try:
        series = source.fetch(symbol, interval, None)
    except SourceError as exc:
        raise SmokeError(f"crypto_showcase: {exc.message}") from None
    except (TypeError, ValueError) as exc:
        raise SmokeError(f"crypto_showcase: response shape changed ({exc})") from None
    return _probe("crypto_showcase", symbol, source.badge, series)


def _print(probe: Probe) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(probe.last_candle_utc))
    lag = "unknown" if probe.lag_seconds is None else f"{probe.lag_seconds:.0f}s"
    state = "stale" if probe.stale else "fresh"
    print(
        f"ok {probe.provider} {probe.symbol} badge={probe.badge} "
        f"candles={probe.candles} last={stamp} lag={lag} {state}"
    )
    if probe.message:
        print(f"   note: {probe.message}")


def main(
    argv: list[str] | None = None,
    *,
    nse_probe: Callable[..., Probe] = probe_nse,
    crypto_probe: Callable[..., Probe] = probe_crypto,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("nse", "kraken", "both"),
        default="both",
        help="which real provider to call (default: both)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        choices=INTERVALS,
        default=1,
        help="candle interval in minutes (default: 1)",
    )
    parser.add_argument("--symbol", default="NSE:RELIANCE", help="delayed-NSE symbol")
    parser.add_argument("--pair", default="BTC", choices=("BTC", "ETH"), help="crypto pair")
    args = parser.parse_args(argv)

    checks: list[tuple[str, Callable[[], Probe]]] = []
    if args.provider in ("nse", "both"):
        checks.append(
            ("nse_delayed", lambda: nse_probe(symbol=args.symbol, interval=args.interval))
        )
    if args.provider in ("kraken", "both"):
        checks.append(
            ("crypto_showcase", lambda: crypto_probe(symbol=args.pair, interval=args.interval))
        )

    failures = 0
    for name, check in checks:
        try:
            _print(check())
        except SmokeError as exc:
            failures += 1
            print(f"FAIL {exc}")
        except Exception as exc:  # a manual tool must say why it stopped
            failures += 1
            print(f"FAIL {name}: unexpected {type(exc).__name__}")

    print(f"{len(checks) - failures}/{len(checks)} providers ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
