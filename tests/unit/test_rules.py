"""Per-rule behaviour and the plugin registry contract."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest import SESSION_DAY, make_frame
from kite_alerts.config.defaults import RULE_IDS
from kite_alerts.config.settings import RuleSettings, StrategyParams
from kite_alerts.core.calendar import SessionWindow
from kite_alerts.core.models import RuleVote
from kite_alerts.strategy.base import RuleContext, build_default_registry
from kite_alerts.strategy.rules import (
    EmaCrossRule,
    MacdCrossRule,
    OpeningRangeRule,
    OpenInterestRule,
    RsiZoneRule,
)


def context(frame: pd.DataFrame, params: StrategyParams | None = None) -> RuleContext:
    return RuleContext(
        symbol="NSE:TEST",
        frame=frame,
        params=params or StrategyParams(),
        session=SessionWindow(),
    )


def test_registry_order_matches_canonical_ids() -> None:
    registry = build_default_registry()
    assert registry.ids == RULE_IDS
    assert len(registry) == 5


def test_registry_rejects_duplicates_and_unknown() -> None:
    registry = build_default_registry()
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(EmaCrossRule())
    with pytest.raises(KeyError):
        registry.get("does_not_exist")
    assert "ema_cross" in registry


def test_registry_enabled_respects_config() -> None:
    params = StrategyParams(
        rules={
            "ema_cross": RuleSettings(enabled=False),
            "rsi_zone": RuleSettings(enabled=True),
            "macd": RuleSettings(enabled=False),
            "orb": RuleSettings(enabled=False),
            "oi": RuleSettings(enabled=False),
        },
        min_agree=1,
    )
    enabled = build_default_registry().enabled(params)
    assert [rule.id for rule, _ in enabled] == ["rsi_zone"]


class TestEmaCrossRule:
    def test_bullish_cross(self) -> None:
        # Flat, then a two-bar jump: the cross is inside the 5-bar lookback.
        prices = [100.0] * 45 + [100.0, 104.0, 108.0]
        vote = EmaCrossRule().evaluate(context(make_frame(prices)))
        assert isinstance(vote, RuleVote)
        assert vote.vote == 1
        assert vote.reason == "EMA9 crossed above EMA21"

    def test_bearish_cross(self) -> None:
        prices = [100.0] * 45 + [100.0, 96.0, 92.0]
        vote = EmaCrossRule().evaluate(context(make_frame(prices)))
        assert vote is not None
        assert vote.vote == -1
        assert vote.reason == "EMA9 crossed below EMA21"

    def test_no_cross_is_neutral(self) -> None:
        prices = np.linspace(100, 101, 60)
        vote = EmaCrossRule().evaluate(context(make_frame(prices)))
        assert vote is not None
        assert vote.vote == 0

    def test_unavailable_without_enough_bars(self) -> None:
        vote = EmaCrossRule().evaluate(context(make_frame([100.0, 101.0])))
        assert vote is None


def sawtooth(up: float, down: float, cycles: int = 40, start: float = 100.0) -> list[float]:
    """+up/-down pattern: average gain/loss ratio sets RSI deterministically."""
    prices = [start]
    for _ in range(cycles):
        prices.append(prices[-1] + up)
        prices.append(prices[-1] - down)
    return prices


class TestRsiZoneRule:
    def test_bullish_zone(self) -> None:
        # +2/-1 -> RS = 2 -> RSI ~ 66.7, inside [55, 75].
        vote = RsiZoneRule().evaluate(context(make_frame(sawtooth(2.0, 1.0))))
        assert vote is not None
        assert vote.vote == 1
        assert vote.reason.startswith("RSI ")
        assert vote.reason.endswith("in bullish zone")

    def test_bearish_zone(self) -> None:
        # +1/-2 -> RS = 0.5 -> RSI ~ 33.3, inside [25, 45].
        vote = RsiZoneRule().evaluate(context(make_frame(sawtooth(1.0, 2.0))))
        assert vote is not None
        assert vote.vote == -1
        assert vote.reason.endswith("in bearish zone")

    def test_neutral_between_zones(self) -> None:
        # +1/-1 -> RS = 1 -> RSI ~ 50, outside both zones.
        vote = RsiZoneRule().evaluate(context(make_frame(sawtooth(1.0, 1.0))))
        assert vote is not None
        assert vote.vote == 0


class TestMacdRule:
    def test_rising_histogram_votes_up(self) -> None:
        prices = np.r_[np.linspace(100, 99, 40), np.linspace(99, 108, 20)]
        vote = MacdCrossRule().evaluate(context(make_frame(prices)))
        assert vote is not None
        assert vote.vote in (0, 1)

    def test_falling_histogram_votes_down(self) -> None:
        prices = np.r_[np.linspace(100, 108, 40), np.linspace(108, 96, 20)]
        vote = MacdCrossRule().evaluate(context(make_frame(prices)))
        assert vote is not None
        assert vote.vote in (0, -1)

    def test_unavailable_before_signal_is_ready(self) -> None:
        vote = MacdCrossRule().evaluate(context(make_frame(np.linspace(100, 102, 20))))
        assert vote is None


class TestOpeningRangeRule:
    def test_breakout(self) -> None:
        prices = np.r_[np.full(15, 100.0), np.linspace(100, 104, 10)]
        vote = OpeningRangeRule().evaluate(context(make_frame(prices)))
        assert vote is not None
        assert vote.vote == 1
        assert vote.reason.startswith("ORB breakout above")

    def test_breakdown(self) -> None:
        prices = np.r_[np.full(15, 100.0), np.linspace(100, 96, 10)]
        vote = OpeningRangeRule().evaluate(context(make_frame(prices)))
        assert vote is not None
        assert vote.vote == -1
        assert vote.reason.startswith("ORB breakdown below")

    def test_neutral_before_range_completes(self) -> None:
        prices = np.linspace(100, 101, 10)
        vote = OpeningRangeRule().evaluate(context(make_frame(prices)))
        assert vote is not None
        assert vote.vote == 0

    def test_buffer_requires_a_bigger_breakout(self) -> None:
        prices = np.r_[np.full(15, 100.0), np.linspace(100, 100.4, 5)]
        params = StrategyParams(orb_buffer_pct=1.0)
        vote = OpeningRangeRule().evaluate(context(make_frame(prices), params))
        assert vote is not None
        assert vote.vote == 0


class TestOpenInterestRule:
    def frame(self, oi_end: float, price_end: float) -> pd.DataFrame:
        prices = np.r_[np.full(10, 100.0), [price_end]]
        oi = np.r_[np.full(10, 1_000_000.0), [oi_end]]
        return make_frame(prices, oi=oi)

    def test_long_buildup(self) -> None:
        vote = OpenInterestRule().evaluate(context(self.frame(1_010_000, 101.0)))
        assert vote is not None
        assert vote.vote == 1
        assert "long buildup" in vote.reason

    def test_short_buildup(self) -> None:
        vote = OpenInterestRule().evaluate(context(self.frame(1_010_000, 99.0)))
        assert vote is not None
        assert vote.vote == -1
        assert "short buildup" in vote.reason

    def test_short_covering(self) -> None:
        vote = OpenInterestRule().evaluate(context(self.frame(990_000, 101.0)))
        assert vote is not None
        assert vote.vote == 1
        assert "short covering" in vote.reason

    def test_long_unwinding(self) -> None:
        vote = OpenInterestRule().evaluate(context(self.frame(990_000, 99.0)))
        assert vote is not None
        assert vote.vote == -1
        assert "long unwinding" in vote.reason

    def test_small_change_is_neutral(self) -> None:
        vote = OpenInterestRule().evaluate(context(self.frame(1_000_100, 100.1)))
        assert vote is not None
        assert vote.vote == 0

    def test_unavailable_for_equities(self) -> None:
        frame = make_frame(np.full(20, 100.0))
        frame["oi"] = 0.0
        assert OpenInterestRule().evaluate(context(frame)) is None

    def test_disabled_returns_unavailable(self) -> None:
        params = StrategyParams(oi_enabled=False)
        assert OpenInterestRule().evaluate(context(self.frame(1_010_000, 101.0), params)) is None

    def test_short_frame_is_unavailable(self) -> None:
        frame = make_frame([100.0, 100.5], oi=[1e6, 1.01e6])
        assert OpenInterestRule().evaluate(context(frame)) is None


def test_rules_are_pure() -> None:
    """Evaluating must not mutate the frame it is given."""
    frame = make_frame(np.linspace(100, 110, 60))
    before = frame.copy(deep=True)
    for rule in build_default_registry():
        rule.evaluate(context(frame))
    pd.testing.assert_frame_equal(frame, before)


def test_session_day_is_a_weekday() -> None:
    assert pd.Timestamp(SESSION_DAY).weekday() == 0
