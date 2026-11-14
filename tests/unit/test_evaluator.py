"""Voting semantics: the contract that must not change.

A BUY/SELL fires when at least ``min_agree`` rules agree and none disagree, with
``min_agree`` capped to the rules that could be evaluated. Weights and required
rules are opt-in and must be no-ops at their defaults.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from conftest import make_frame
from kite_alerts.config.settings import RuleSettings, StrategyParams
from kite_alerts.core.models import Side
from kite_alerts.strategy.base import RuleRegistry
from kite_alerts.strategy.evaluator import Evaluation, evaluate


class StubRule:
    """Deterministic stand-in for a real rule."""

    requires_oi = False

    def __init__(self, rule_id: str, vote: int | None, reason: str | None = None) -> None:
        self.id = rule_id
        self.label = rule_id
        self._vote = vote
        self._reason = reason if reason is not None else f"{rule_id} says {vote}"

    def evaluate(self, ctx) -> object:
        if self._vote is None:
            return None
        from kite_alerts.core.models import RuleVote

        return RuleVote(self.id, self._vote, self._reason if self._vote else "")


class BoomRule:
    id = "ema_cross"
    label = "boom"
    requires_oi = False

    def evaluate(self, ctx) -> object:
        raise RuntimeError("rule bug")


def registry_with(votes: dict[str, int | None]) -> RuleRegistry:
    return RuleRegistry([StubRule(rule_id, vote) for rule_id, vote in votes.items()])


def frame(bars: int = 60) -> pd.DataFrame:
    return make_frame(np.linspace(100, 102, bars))


def test_four_agreeing_and_one_neutral_fires() -> None:
    params = StrategyParams()
    registry = registry_with({"ema_cross": 1, "rsi_zone": 1, "macd": 1, "orb": 1, "oi": 0})
    alert = evaluate("NSE:TEST", frame(), params, registry=registry)
    assert alert is not None
    assert alert.side is Side.BUY
    assert alert.rules_available == 5
    assert len(alert.reasons) == 4
    assert [reason.split(" says")[0] for reason in alert.reasons] == [
        "ema_cross",
        "rsi_zone",
        "macd",
        "orb",
    ]


def test_any_disagreement_blocks_the_alert() -> None:
    params = StrategyParams()
    registry = registry_with({"ema_cross": 1, "rsi_zone": 1, "macd": 1, "orb": 1, "oi": -1})
    assert evaluate("NSE:TEST", frame(), params, registry=registry) is None


def test_not_enough_agreement() -> None:
    params = StrategyParams()
    registry = registry_with({"ema_cross": 1, "rsi_zone": 1, "macd": 1, "orb": 0, "oi": 0})
    assert evaluate("NSE:TEST", frame(), params, registry=registry) is None


def test_need_is_capped_to_evaluable_rules() -> None:
    """Legacy behaviour: with only 3 evaluable rules, 3 agreeing rules can fire."""
    params = StrategyParams()
    registry = registry_with({"ema_cross": 1, "rsi_zone": 1, "macd": 1, "orb": None, "oi": None})
    alert = evaluate("NSE:TEST", frame(), params, registry=registry)
    assert alert is not None
    assert alert.rules_available == 3


def test_min_available_rules_blocks_degraded_alerts() -> None:
    # 2 evaluable rules would normally be enough (need is capped), but the
    # operator asked for at least 4 before trusting an alert.
    params = StrategyParams(min_agree=4, min_available_rules=4)
    registry = registry_with({"ema_cross": 1, "rsi_zone": 1, "macd": None, "orb": None, "oi": None})
    assert evaluate("NSE:TEST", frame(), params, registry=registry) is None


def test_detail_reports_votes_and_scores() -> None:
    params = StrategyParams()
    registry = registry_with({"ema_cross": 1, "rsi_zone": 0, "macd": -1, "orb": None, "oi": 0})
    result = evaluate("NSE:TEST", frame(), params, registry=registry, detail=True)
    assert isinstance(result, Evaluation)
    assert result.available == 4
    assert result.bull_score == 1.0
    assert result.bear_score == 1.0
    assert result.alert is None
    assert result.degraded is False


def test_warmup_blocks_evaluation() -> None:
    params = StrategyParams()
    registry = registry_with({"ema_cross": 1, "rsi_zone": 1, "macd": 1, "orb": 1, "oi": 1})
    short = frame(bars=10)
    assert evaluate("NSE:TEST", short, params, registry=registry) is None
    detail = evaluate("NSE:TEST", short, params, registry=registry, detail=True)
    assert isinstance(detail, Evaluation)
    assert detail.votes == ()


def test_weights_scale_the_threshold() -> None:
    params = StrategyParams(
        min_agree=3,
        rules={
            "ema_cross": RuleSettings(weight=2.0),
            "rsi_zone": RuleSettings(weight=1.0),
            "macd": RuleSettings(),
            "orb": RuleSettings(enabled=False),
            "oi": RuleSettings(enabled=False),
        },
    )
    registry = registry_with({"ema_cross": 1, "rsi_zone": 1, "macd": 0})
    alert = evaluate("NSE:TEST", frame(), params, registry=registry)
    assert alert is not None  # 2.0 + 1.0 >= min_agree of 3


def test_required_rule_must_agree() -> None:
    params = StrategyParams(
        min_agree=1,
        rules={
            "ema_cross": RuleSettings(required=True),
            "rsi_zone": RuleSettings(),
            "macd": RuleSettings(),
            "orb": RuleSettings(),
            "oi": RuleSettings(),
        },
    )
    registry = registry_with({"ema_cross": 0, "rsi_zone": 1, "macd": 1, "orb": 1, "oi": 1})
    assert evaluate("NSE:TEST", frame(), params, registry=registry) is None


def test_sell_side_uses_bearish_reasons() -> None:
    params = StrategyParams()
    registry = registry_with({"ema_cross": -1, "rsi_zone": -1, "macd": -1, "orb": -1, "oi": -1})
    alert = evaluate("NSE:TEST", frame(), params, registry=registry)
    assert alert is not None
    assert alert.side is Side.SELL
    assert alert.message().startswith("🔴 SELL NSE:TEST @")


def test_rule_exception_is_treated_as_unavailable() -> None:
    params = StrategyParams(min_agree=3)
    registry = RuleRegistry([BoomRule()])
    # `enabled()` only returns rules present in params.rules; ema_cross is.
    alert = evaluate("NSE:TEST", frame(), params, registry=registry)
    assert alert is None


def test_alert_message_matches_documented_format() -> None:
    params = StrategyParams()
    registry = registry_with({"ema_cross": 1, "rsi_zone": 1, "macd": 1, "orb": 1, "oi": 0})
    alert = evaluate("NSE:TEST", frame(), params, registry=registry)
    assert alert is not None
    text = alert.message()
    assert text == (
        "🟢 BUY NSE:TEST @ 102.00 [10:14] — ema_cross says 1; rsi_zone says 1; "
        "macd says 1; orb says 1"
    )


def test_params_snapshot_is_recorded() -> None:
    params = StrategyParams()
    registry = registry_with({"ema_cross": 1, "rsi_zone": 1, "macd": 1, "orb": 1, "oi": 1})
    alert = evaluate("NSE:TEST", frame(), params, registry=registry)
    assert alert is not None
    assert alert.params["min_agree"] == params.min_agree
    assert len(alert.reasons) == 5  # every stub voted bullish with a reason
