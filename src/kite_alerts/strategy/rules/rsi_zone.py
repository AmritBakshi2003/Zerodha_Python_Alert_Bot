"""RSI zone rule (prototype logic, unchanged)."""

from __future__ import annotations

import pandas as pd

from ...core.models import RuleVote
from ...indicators import rsi
from ..base import RuleContext


class RsiZoneRule:
    id = "rsi_zone"
    label = "RSI zone"
    requires_oi = False

    def evaluate(self, ctx: RuleContext) -> RuleVote | None:
        params = ctx.params
        value = rsi(ctx.frame["close"], params.rsi_period, seed=params.indicator_seed).iloc[-1]
        if pd.isna(value):
            return None
        if params.rsi_buy_min <= value <= params.rsi_buy_max:
            return RuleVote(self.id, 1, f"RSI {value:.1f} in bullish zone")
        if params.rsi_sell_min <= value <= params.rsi_sell_max:
            return RuleVote(self.id, -1, f"RSI {value:.1f} in bearish zone")
        return RuleVote(self.id, 0)
