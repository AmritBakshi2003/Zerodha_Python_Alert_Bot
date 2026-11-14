"""EMA fast/slow cross rule (prototype logic, unchanged)."""

from __future__ import annotations

import pandas as pd

from ...core.models import RuleVote
from ...indicators import ema
from ..base import RuleContext


class EmaCrossRule:
    id = "ema_cross"
    label = "EMA cross"
    requires_oi = False

    def evaluate(self, ctx: RuleContext) -> RuleVote | None:
        params = ctx.params
        close = ctx.frame["close"]
        fast = ema(close, params.ema_fast, seed=params.indicator_seed)
        slow = ema(close, params.ema_slow, seed=params.indicator_seed)
        diff = fast - slow
        if pd.isna(diff.iloc[-1]):
            return None

        # A cross is "recent" if the sign differed at any point in the lookback
        # window (excluding the current bar). Prototype semantics, preserved.
        recent = diff.iloc[-(params.ema_cross_lookback + 1) : -1]
        if diff.iloc[-1] > 0 and (recent <= 0).any():
            return RuleVote(
                self.id,
                1,
                f"EMA{params.ema_fast} crossed above EMA{params.ema_slow}",
            )
        if diff.iloc[-1] < 0 and (recent >= 0).any():
            return RuleVote(
                self.id,
                -1,
                f"EMA{params.ema_fast} crossed below EMA{params.ema_slow}",
            )
        return RuleVote(self.id, 0)
