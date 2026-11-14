"""Canonical rule identifiers, in evaluation (and therefore reason) order.

The order matters: alert reason text is rendered in this order, which keeps the
demo output stable. ``tests/unit/test_rules_registry.py`` asserts the runtime
registry matches this tuple exactly.
"""

from __future__ import annotations

from typing import Final

RULE_IDS: Final[tuple[str, ...]] = ("ema_cross", "rsi_zone", "macd", "orb", "oi")

#: Env prefix per rule, e.g. ``RULE_EMA_CROSS_ENABLED``.
RULE_ENV_PREFIX: Final[str] = "RULE_"

#: Session length in minutes (09:15-15:30) used by config sanity warnings.
SESSION_MINUTES: Final[int] = 375
