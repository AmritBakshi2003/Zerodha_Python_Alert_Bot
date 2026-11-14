"""The voting evaluator.

Semantics are the prototype's, deliberately:

* Every enabled rule votes +1 / -1 / 0, or is *unavailable* (``None``).
* An alert fires when the agreeing weight is at least ``min_agree`` **and**
  nothing disagrees.
* ``min_agree`` is capped to the weight of the rules that were evaluable, so a
  missing indicator (e.g. no OI data) cannot make an alert impossible.

Two additions are opt-in and no-ops by default:

* ``rules.<id>.weight`` — scales a rule's contribution. With the default 1.0,
  "score" is exactly "number of agreeing rules".
* ``rules.<id>.required`` — a mandatory rule must agree, or no alert fires.
* ``min_available_rules`` — refuse to alert on a degraded rule set. 0 = legacy
  behaviour; the alert is still logged/recorded with ``rules_available``.
"""

from __future__ import annotations

import logging
from typing import Literal, overload

import pandas as pd

from ..config.defaults import RULE_IDS
from ..config.settings import StrategyParams
from ..core.calendar import SessionWindow
from ..core.models import Alert, RuleVote, Side
from .base import RuleContext, RuleRegistry, build_default_registry

log = logging.getLogger(__name__)

_REGISTRY: RuleRegistry | None = None


def default_registry() -> RuleRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_default_registry()
    return _REGISTRY


class Evaluation:
    """Full, auditable result of one evaluation (not just the alert)."""

    __slots__ = ("alert", "available", "bear_score", "bull_score", "degraded", "votes")

    def __init__(
        self,
        *,
        alert: Alert | None,
        votes: tuple[RuleVote, ...],
        available: int,
        bull_score: float,
        bear_score: float,
        degraded: bool,
    ) -> None:
        self.alert = alert
        self.votes = votes
        self.available = available
        self.bull_score = bull_score
        self.bear_score = bear_score
        self.degraded = degraded

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Evaluation(alert={self.alert!r}, available={self.available}, "
            f"bull={self.bull_score}, bear={self.bear_score}, degraded={self.degraded})"
        )


@overload
def evaluate(
    symbol: str,
    frame: pd.DataFrame,
    params: StrategyParams,
    *,
    session: SessionWindow | None = None,
    registry: RuleRegistry | None = None,
    detail: Literal[False] = False,
) -> Alert | None: ...


@overload
def evaluate(
    symbol: str,
    frame: pd.DataFrame,
    params: StrategyParams,
    *,
    session: SessionWindow | None = None,
    registry: RuleRegistry | None = None,
    detail: Literal[True],
) -> Evaluation: ...


def evaluate(
    symbol: str,
    frame: pd.DataFrame,
    params: StrategyParams,
    *,
    session: SessionWindow | None = None,
    registry: RuleRegistry | None = None,
    detail: bool = False,
) -> Alert | Evaluation | None:
    """Evaluate ``frame`` and return an ``Alert`` (or ``None``).

    With ``detail=True`` an ``Evaluation`` is returned instead, carrying every
    vote for the audit log and for tests that assert voting behaviour.
    """
    active_registry = registry or default_registry()
    active_session = session or SessionWindow()
    enabled = active_registry.enabled(params)

    if len(frame) < params.warmup_bars:
        return (
            Evaluation(
                alert=None, votes=(), available=0, bull_score=0.0, bear_score=0.0, degraded=False
            )
            if detail
            else None
        )

    ctx = RuleContext(symbol=symbol, frame=frame, params=params, session=active_session)
    votes: list[RuleVote] = []
    for rule, rule_settings in enabled:
        try:
            result = rule.evaluate(ctx)
        except Exception:  # a rule bug must never take down the feed
            log.exception("rule %s failed for %s", rule.id, symbol)
            result = None
        if result is None:
            continue
        votes.append(
            RuleVote(
                rule_id=result.rule_id,
                vote=result.vote,
                reason=result.reason,
                weight=rule_settings.weight,
            )
        )

    available = len(votes)
    if available == 0:
        return (
            Evaluation(
                alert=None, votes=(), available=0, bull_score=0.0, bear_score=0.0, degraded=True
            )
            if detail
            else None
        )

    bull = [vote for vote in votes if vote.vote > 0]
    bear = [vote for vote in votes if vote.vote < 0]
    bull_score = sum(vote.weight for vote in bull)
    bear_score = sum(vote.weight for vote in bear)
    weighted_available = sum(vote.weight for vote in votes)
    need = min(max(params.min_agree, 1), weighted_available)
    degraded = params.min_available_rules > 0 and available < params.min_available_rules

    alert: Alert | None = None
    if not degraded:
        required_ok = _required_rules_satisfied(enabled, votes)
        if bull_score >= need and not bear and required_ok:
            alert = _build_alert(symbol, frame, params, Side.BUY, bull, available, enabled)
        elif bear_score >= need and not bull and required_ok:
            alert = _build_alert(symbol, frame, params, Side.SELL, bear, available, enabled)

    if degraded:
        log.warning(
            "%s: only %d rule(s) evaluable (< MIN_AVAILABLE_RULES=%d); alert suppressed",
            symbol,
            available,
            params.min_available_rules,
        )

    if detail:
        return Evaluation(
            alert=alert,
            votes=tuple(votes),
            available=available,
            bull_score=bull_score,
            bear_score=bear_score,
            degraded=degraded,
        )
    return alert


def _required_rules_satisfied(
    enabled: tuple[tuple[object, object], ...], votes: list[RuleVote]
) -> bool:
    """Every rule marked ``required`` must have voted and be part of the majority."""
    from ..config.settings import RuleSettings

    required_ids = {
        rule_id
        for rule_id in RULE_IDS
        for _rule, settings in enabled
        if getattr(_rule, "id", None) == rule_id
        and isinstance(settings, RuleSettings)
        and settings.required
    }
    if not required_ids:
        return True
    return all(vote.rule_id in required_ids for vote in votes if vote.weight > 0)


def _build_alert(
    symbol: str,
    frame: pd.DataFrame,
    params: StrategyParams,
    side: Side,
    agreeing: list[RuleVote],
    available: int,
    enabled: tuple[tuple[object, object], ...],
) -> Alert:
    reasons = tuple(vote.reason for vote in agreeing if vote.reason)
    if available < len(enabled):
        log.warning(
            "%s: alert on %d/%d evaluable rules (reduced evidence)",
            symbol,
            available,
            len(enabled),
        )
    return Alert(
        symbol=symbol,
        side=side,
        ts=frame.index[-1],
        price=float(frame["close"].iloc[-1]),
        reasons=reasons,
        rules_available=available,
        params=params.snapshot(),
        rules_used=tuple(vote.rule_id for vote in agreeing),
    )
