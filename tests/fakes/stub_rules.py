"""Deterministic rules for tests that must not depend on indicator maths."""

from __future__ import annotations

from kite_alerts.core.models import RuleVote
from kite_alerts.strategy.base import RuleContext, RuleRegistry


class StubRule:
    requires_oi = False

    def __init__(self, rule_id: str, vote: int | None) -> None:
        self.id = rule_id
        self.label = rule_id
        self._vote = vote

    def evaluate(self, ctx: RuleContext) -> RuleVote | None:
        if self._vote is None:
            return None
        reason = f"{self.id} agrees" if self._vote else ""
        return RuleVote(self.id, self._vote, reason)


def stub_registry(votes: dict[str, int | None]) -> RuleRegistry:
    return RuleRegistry([StubRule(rule_id, vote) for rule_id, vote in votes.items()])


def all_bullish() -> RuleRegistry:
    return stub_registry({"ema_cross": 1, "rsi_zone": 1, "macd": 1, "orb": 1, "oi": 1})


def all_bearish() -> RuleRegistry:
    return stub_registry({"ema_cross": -1, "rsi_zone": -1, "macd": -1, "orb": -1, "oi": -1})
