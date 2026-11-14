"""MACD line/signal/histogram rule (prototype logic, unchanged)."""

from __future__ import annotations

import pandas as pd

from ...core.models import RuleVote
from ...indicators import macd
from ..base import RuleContext


class MacdCrossRule:
    id = "macd"
    label = "MACD"
    requires_oi = False

    def evaluate(self, ctx: RuleContext) -> RuleVote | None:
        params = ctx.params
        line, signal, hist = macd(
            ctx.frame["close"],
            params.macd_fast,
            params.macd_slow,
            params.macd_signal,
            seed=params.indicator_seed,
        )
        if pd.isna(signal.iloc[-1]) or len(hist) < 2:
            return None
        if line.iloc[-1] > signal.iloc[-1] and hist.iloc[-1] > hist.iloc[-2]:
            return RuleVote(self.id, 1, "MACD above signal, histogram rising")
        if line.iloc[-1] < signal.iloc[-1] and hist.iloc[-1] < hist.iloc[-2]:
            return RuleVote(self.id, -1, "MACD below signal, histogram falling")
        return RuleVote(self.id, 0)
