"""Opening-range breakout rule (prototype logic, tz-aware range helper)."""

from __future__ import annotations

from ...core.models import RuleVote
from ...indicators import opening_range
from ..base import RuleContext


class OpeningRangeRule:
    id = "orb"
    label = "Opening range"
    requires_oi = False

    def evaluate(self, ctx: RuleContext) -> RuleVote | None:
        params = ctx.params
        rng = opening_range(
            ctx.frame,
            params.orb_minutes,
            session_open=ctx.session.open_time,
        )
        if rng is None:
            # Range not formed yet (or no 09:15 bars): neutral, as in the prototype.
            return RuleVote(self.id, 0)
        close = float(ctx.frame["close"].iloc[-1])
        buffer = params.orb_buffer_pct / 100.0
        if close > rng.high * (1 + buffer):
            return RuleVote(self.id, 1, f"ORB breakout above {rng.high:.2f}")
        if close < rng.low * (1 - buffer):
            return RuleVote(self.id, -1, f"ORB breakdown below {rng.low:.2f}")
        return RuleVote(self.id, 0)
