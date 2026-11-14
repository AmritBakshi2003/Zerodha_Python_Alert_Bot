"""The five built-in rules, in the order their reasons appear in an alert."""

from __future__ import annotations

from .ema_cross import EmaCrossRule
from .macd_cross import MacdCrossRule
from .oi import OpenInterestRule
from .orb import OpeningRangeRule
from .rsi_zone import RsiZoneRule

#: Registry order == alert reason-text order (must match config.defaults.RULE_IDS).
RULES = (
    EmaCrossRule(),
    RsiZoneRule(),
    MacdCrossRule(),
    OpeningRangeRule(),
    OpenInterestRule(),
)

__all__ = [
    "RULES",
    "EmaCrossRule",
    "MacdCrossRule",
    "OpenInterestRule",
    "OpeningRangeRule",
    "RsiZoneRule",
]
