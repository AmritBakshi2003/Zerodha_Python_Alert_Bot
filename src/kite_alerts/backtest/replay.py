"""Replay / backtest.

This runs the *same* ``Engine`` and the *same* rules as live, one closed candle at
a time, so parameter changes can be compared against the alerts the bot would
actually have produced. It is analysis, not a performance guarantee: forward
returns are descriptive statistics on a small sample, with no costs, slippage or
position sizing.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..config.settings import StrategyParams
from ..core.calendar import TradingCalendar
from ..core.clock import Clock
from ..core.models import Alert, Candle, Side
from ..engine.engine import Engine
from ..persistence.store import InMemoryAlertStore

log = logging.getLogger(__name__)

DEFAULT_HORIZONS: tuple[int, ...] = (5, 15, 30)
_COLUMN_ALIASES = {
    "date": "time",
    "timestamp": "time",
    "datetime": "time",
    "last_price": "close",
    "ltp": "close",
    "volume_traded": "volume",
    "open_interest": "oi",
}


class _Collector:
    """Alert sink that only records (no notification)."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def submit(self, alert: Alert) -> None:
        self.alerts.append(alert)


@dataclass
class ReplayResult:
    symbol: str
    bars: int
    alerts: list[Alert] = field(default_factory=list)
    forward: dict[str, dict[int, float | None]] = field(default_factory=dict)

    @property
    def buys(self) -> int:
        return sum(1 for alert in self.alerts if alert.side is Side.BUY)

    @property
    def sells(self) -> int:
        return sum(1 for alert in self.alerts if alert.side is Side.SELL)


def load_csv(path: str | Path, *, symbol: str) -> pd.DataFrame:
    """Load OHLC(V)(OI) candles from CSV. Naive timestamps are assumed IST."""
    frame = pd.read_csv(path)
    frame = frame.rename(columns={k: v for k, v in _COLUMN_ALIASES.items() if k in frame.columns})
    if "time" not in frame.columns:
        raise ValueError(f"{path}: CSV needs a time/date column (got {list(frame.columns)})")
    for column in ("open", "high", "low", "close"):
        if column not in frame.columns:
            raise ValueError(f"{path}: CSV is missing the {column!r} column")

    from ..core.clock import to_ist_ts

    frame.index = pd.DatetimeIndex([to_ist_ts(value) for value in frame["time"]], name="time")
    if "volume" not in frame.columns:
        frame["volume"] = 0
    if "oi" not in frame.columns:
        frame["oi"] = 0.0
    out = frame[["open", "high", "low", "close", "volume", "oi"]].astype(float)
    out["volume"] = out["volume"].astype("int64")
    return cast(pd.DataFrame, out[~out.index.duplicated(keep="last")].sort_index())


def replay_frame(
    symbol: str,
    frame: pd.DataFrame,
    params: StrategyParams,
    *,
    calendar: TradingCalendar | None = None,
    warmup_bars: int | None = None,
) -> list[Alert]:
    """Feed ``frame`` through the live engine and return the alerts it fires."""
    if frame.empty:
        return []
    calendar = calendar or TradingCalendar()
    collector = _Collector()
    store = InMemoryAlertStore()
    engine = Engine(
        params_for=lambda _symbol: params,
        store=store,
        sink=collector,
        calendar=calendar,
    )
    seed_bars = (
        warmup_bars
        if warmup_bars is not None
        else max(params.warmup_bars, params.min_bars_required)
    )
    seed_bars = min(seed_bars, len(frame))
    engine.seed(symbol, frame.iloc[:seed_bars])

    for ts, row in frame.iloc[seed_bars:].iterrows():
        engine.on_candle(
            Candle(
                symbol=symbol,
                time=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row.get("volume", 0) or 0),
                oi=float(row.get("oi", 0.0) or 0.0),
            )
        )
    return collector.alerts


def forward_return(frame: pd.DataFrame, ts: pd.Timestamp, side: Side, minutes: int) -> float | None:
    """Signed percentage move ``minutes`` after ``ts`` (None when data is missing)."""
    try:
        entry = float(cast(float, frame.loc[ts, "close"]))
    except KeyError:
        return None
    if entry <= 0:
        return None
    target = ts + pd.Timedelta(minutes=minutes)
    later = frame[frame.index >= target]
    if later.empty:
        return None
    exit_price = float(later["close"].iloc[0])
    move = (exit_price / entry - 1.0) * 100.0
    return move if side is Side.BUY else -move


def build_result(
    symbol: str,
    frame: pd.DataFrame,
    alerts: Sequence[Alert],
    *,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
) -> ReplayResult:
    forward: dict[str, dict[int, float | None]] = {}
    for alert in alerts:
        forward[alert.dedup_key] = {
            int(h): forward_return(frame, alert.ts, alert.side, int(h)) for h in horizons
        }
    return ReplayResult(symbol=symbol, bars=len(frame), alerts=list(alerts), forward=forward)


def build_report(
    result: ReplayResult,
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    params: StrategyParams | None = None,
) -> str:
    """Human-readable report. Labelled as analysis, explicitly."""
    lines = [
        "=" * 78,
        f"Replay report — {result.symbol}",
        "ANALYSIS ONLY: same engine, same rules, closed candles. No costs, no slippage,",
        "no position sizing. This is not a performance guarantee or investment advice.",
        "=" * 78,
        f"bars: {result.bars}   alerts: {len(result.alerts)}   "
        f"(BUY {result.buys}, SELL {result.sells})",
    ]
    if params is not None:
        lines.append(
            f"params: min_agree={params.min_agree} cooldown={params.cooldown_minutes}m ema={params.ema_fast}/{params.ema_slow} rsi={params.rsi_period} orb={params.orb_minutes}m seed={params.indicator_seed}"
        )
    lines.append("")
    header = f"{'time':<17}{'side':<6}{'price':>10}" + "".join(
        f"{'+' + str(h) + 'm':>10}" for h in horizons
    )
    lines.append(header)
    lines.append("-" * len(header))
    for alert in result.alerts:
        cells = []
        for h in horizons:
            value = result.forward.get(alert.dedup_key, {}).get(h)
            cells.append(f"{value:+.2f}%" if value is not None else "n/a")
        lines.append(
            f"{alert.ts:%Y-%m-%d %H:%M}  {alert.side.value:<6}{alert.price:>10.2f}"
            + "".join(f"{cell:>10}" for cell in cells)
        )

    lines.append("")
    for horizon in horizons:
        values = [
            value
            for mapping in result.forward.values()
            if (value := mapping.get(horizon)) is not None
        ]
        if not values:
            lines.append(f"+{horizon}m: no forward data")
            continue
        wins = sum(1 for value in values if value > 0)
        lines.append(
            f"+{horizon}m: n={len(values)} mean={sum(values) / len(values):+.2f}% "
            f"median={pd.Series(values).median():+.2f}% hit_rate={wins / len(values):.0%} "
            f"best={max(values):+.2f}% worst={min(values):+.2f}%"
        )
    return "\n".join(lines)


def replay_kite_history(
    kite: object,
    *,
    symbol: str,
    token: int,
    params: StrategyParams,
    days: int = 5,
    continuous: bool = False,
    calendar: TradingCalendar | None = None,
    clock: object | None = None,
) -> ReplayResult:
    """Replay candles fetched from Kite (read-only market-data call)."""
    from ..config.settings import ResilienceSettings
    from ..data.history import HistoryClient

    history = HistoryClient(kite, settings=ResilienceSettings(), clock=cast(Clock | None, clock))
    frame = history.fetch(token, days=days, continuous=continuous)
    if frame.empty:
        raise ValueError(f"no historical candles returned for {symbol}")
    alerts = replay_frame(symbol, frame, params, calendar=calendar)
    return build_result(symbol, frame, alerts)


def default_horizons_label(horizons: Sequence[int] = DEFAULT_HORIZONS) -> str:
    return ", ".join(f"+{h}m" for h in horizons)


_ = dt  # datetime is part of the public typing surface for callers
