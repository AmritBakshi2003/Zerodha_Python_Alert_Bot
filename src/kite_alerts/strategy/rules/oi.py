"""Open-interest / price behaviour rule (prototype logic, unchanged)."""

from __future__ import annotations

from ...core.models import RuleVote
from ..base import RuleContext


class OpenInterestRule:
    id = "oi"
    label = "Open interest"
    requires_oi = True

    def evaluate(self, ctx: RuleContext) -> RuleVote | None:
        params = ctx.params
        frame = ctx.frame
        lookback = params.oi_lookback

        if (
            not params.oi_enabled
            or "oi" not in frame.columns
            or len(frame) <= lookback
            or frame["oi"].iloc[-1] <= 0
            or frame["oi"].iloc[-1 - lookback] <= 0
        ):
            return None

        change = (frame["oi"].iloc[-1] / frame["oi"].iloc[-1 - lookback] - 1) * 100
        price_change = frame["close"].iloc[-1] - frame["close"].iloc[-1 - lookback]
        threshold = params.oi_min_change_pct

        if change >= threshold:
            if price_change > 0:
                return RuleVote(self.id, 1, f"OI {change:+.2f}% with price up (long buildup)")
            if price_change < 0:
                return RuleVote(self.id, -1, f"OI {change:+.2f}% with price down (short buildup)")
        elif change <= -threshold:
            if price_change > 0:
                return RuleVote(self.id, 1, f"OI {change:+.2f}% with price up (short covering)")
            if price_change < 0:
                return RuleVote(self.id, -1, f"OI {change:+.2f}% with price down (long unwinding)")
        return RuleVote(self.id, 0)
