"""The demo player.

It replays exactly the same synthetic day as ``kite-alerts demo`` through exactly
the same :class:`~kite_alerts.engine.engine.Engine` the live bot uses, records
every candle and alert, and hands the browser a timeline it can play back at
1x/5x/20x/60x.

Hard guarantees:

* alerts come from the engine, so their text is byte-identical to
  ``demo_output.txt`` (asserted by the tests);
* the engine writes to :class:`~kite_alerts.persistence.store.InMemoryAlertStore`
  only. This module never opens the real SQLite store, so demo alerts cannot
  leak into the live alert history;
* every payload is labelled ``DEMO``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import pandas as pd

from ..config.defaults import RULE_IDS
from ..config.settings import StrategyParams
from ..core.calendar import SessionWindow, TradingCalendar
from ..core.models import Alert, Candle, Side
from ..demo import SYMBOL, synthetic_day
from ..engine.engine import Engine
from ..persistence.store import InMemoryAlertStore
from ..strategy.evaluator import evaluate
from .series import ChartSeries, build_chart, chart_time

RULE_LABELS: dict[str, str] = {
    "ema_cross": "EMA",
    "rsi_zone": "RSI",
    "macd": "MACD",
    "orb": "ORB",
    "oi": "OI",
}

#: Playback multipliers offered by the UI.
SPEEDS: tuple[int, ...] = (1, 5, 20, 60)


class _CollectingSink:
    """Engine sink that records alerts instead of sending them anywhere."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def submit(self, alert: Alert) -> None:
        self.alerts.append(alert)


@dataclass(frozen=True, slots=True)
class DemoAlert:
    index: int
    chart_time: int
    ts: str
    side: str
    price: float
    message: str
    reasons: tuple[str, ...]
    rules: tuple[dict[str, str], ...]
    rules_used: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "demo": True,
            "label": "DEMO",
            "index": self.index,
            "time": self.chart_time,
            "ts": self.ts,
            "side": self.side,
            "price": self.price,
            "message": self.message,
            "reasons": list(self.reasons),
            "rules": [dict(rule) for rule in self.rules],
            "rules_used": list(self.rules_used),
        }


@dataclass(frozen=True, slots=True)
class DemoTimeline:
    symbol: str
    session_date: str
    candles: tuple[dict[str, Any], ...]
    orb: dict[str, Any] | None
    alerts: tuple[DemoAlert, ...]

    @property
    def buys(self) -> int:
        return sum(1 for alert in self.alerts if alert.side == Side.BUY.value)

    @property
    def sells(self) -> int:
        return sum(1 for alert in self.alerts if alert.side == Side.SELL.value)

    def payload(self) -> dict[str, Any]:
        return {
            "demo": True,
            "label": "DEMO",
            "symbol": self.symbol,
            "session_date": self.session_date,
            "note": (
                "This is a practice replay of a made-up trading day. "
                "Nothing here is saved to your real alert history."
            ),
            "speeds": list(SPEEDS),
            "candles": list(self.candles),
            "orb": self.orb,
            "alerts": [alert.as_dict() for alert in self.alerts],
            "counts": {
                "buys": self.buys,
                "sells": self.sells,
                "total": len(self.alerts),
            },
            "rule_labels": RULE_LABELS,
            "rule_order": list(RULE_IDS),
        }

    def events(self) -> Iterator[dict[str, Any]]:
        """Candle/alert events in replay order, for ``/api/demo/stream``."""
        by_index: dict[int, list[dict[str, Any]]] = {}
        for alert in self.alerts:
            by_index.setdefault(alert.index, []).append(alert.as_dict())
        for index, candle in enumerate(self.candles):
            yield {"type": "candle", "index": index, "candle": candle}
            for event_alert in by_index.get(index, []):
                yield {"type": "alert", "index": index, "alert": event_alert, "demo": True}
        yield {
            "type": "end",
            "demo": True,
            "counts": {"buys": self.buys, "sells": self.sells, "total": len(self.alerts)},
        }


def _rule_status(vote: int | None, side: Side) -> str:
    if vote is None:
        return "unavailable"
    if vote == 0:
        return "neutral"
    agrees = (vote > 0 and side is Side.BUY) or (vote < 0 and side is Side.SELL)
    return "agree" if agrees else "against"


def _rule_evidence(
    frame: pd.DataFrame, params: StrategyParams, alert: Alert
) -> tuple[dict[str, str], ...]:
    """Re-evaluate the candle that fired the alert to show every rule's vote."""
    window = SessionWindow()
    evaluation = evaluate(SYMBOL, frame, params, session=window, detail=True)
    votes = {vote.rule_id: vote.vote for vote in evaluation.votes}
    return tuple(
        {
            "id": rule_id,
            "label": RULE_LABELS[rule_id],
            "status": _rule_status(votes.get(rule_id), alert.side),
        }
        for rule_id in RULE_IDS
    )


def _replay() -> tuple[ChartSeries, tuple[Alert, ...], pd.DataFrame, StrategyParams]:
    """Run the synthetic day through the production engine (in-memory only)."""
    params = StrategyParams(ema_cross_lookback=5)
    calendar = TradingCalendar()
    sink = _CollectingSink()
    engine = Engine(
        params_for=lambda _symbol: params,
        store=InMemoryAlertStore(),
        sink=sink,
        calendar=calendar,
    )

    data = synthetic_day()
    engine.seed(SYMBOL, data.iloc[:50])  # previous-session candles, no alerts
    for ts, row in data.iloc[50:].iterrows():
        engine.on_candle(
            Candle(
                symbol=SYMBOL,
                time=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
                oi=float(row["oi"]),
            )
        )
    return build_chart(data, params), tuple(sink.alerts), data, params


def _assemble() -> DemoTimeline:
    chart, alerts, frame, params = _replay()
    session = frame[frame.index.date == pd.Timestamp(chart.session_date).date()]
    positions = {ts.isoformat(): index for index, ts in enumerate(session.index)}

    demo_alerts: list[DemoAlert] = []
    for alert in alerts:
        index = positions.get(alert.ts.isoformat())
        if index is None:  # pragma: no cover - synthetic data always contains it
            continue
        position = int(frame.index.get_loc(alert.ts))
        evidence = _rule_evidence(frame.iloc[: position + 1], params, alert)
        demo_alerts.append(
            DemoAlert(
                index=index,
                chart_time=chart_time(alert.ts),
                ts=alert.ts.isoformat(),
                side=alert.side.value,
                price=float(alert.price),
                message=alert.message(),
                reasons=alert.reasons,
                rules=evidence,
                rules_used=alert.rules_used,
            )
        )

    return DemoTimeline(
        symbol=SYMBOL,
        session_date=chart.session_date,
        candles=chart.candles,
        orb=chart.orb,
        alerts=tuple(demo_alerts),
    )


@lru_cache(maxsize=1)
def build_demo_timeline() -> DemoTimeline:
    """Build (once per process) the full demo timeline."""
    return _assemble()


_BUILD_LOCK = threading.Lock()


def warm_demo_timeline() -> DemoTimeline:
    """Thread-safe first build (FastAPI may serve two requests concurrently)."""
    with _BUILD_LOCK:
        return build_demo_timeline()
