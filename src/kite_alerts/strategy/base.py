"""Rule plugin protocol and registry.

Adding a rule is additive: implement ``Rule`` (or a class with the same
attributes), then register it. Enabling/disabling, weighting and marking a rule
as mandatory are configuration concerns, not code edits.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pandas as pd

from ..config.settings import RuleSettings, StrategyParams
from ..core.calendar import SessionWindow
from ..core.models import RuleVote


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Everything a rule may look at. Rules must be pure: no I/O, no mutation."""

    symbol: str
    frame: pd.DataFrame
    params: StrategyParams
    session: SessionWindow


@runtime_checkable
class Rule(Protocol):
    """A single voting rule.

    ``evaluate`` returns:
      * ``RuleVote(vote=+1)`` bullish
      * ``RuleVote(vote=-1)`` bearish
      * ``RuleVote(vote=0)``  no opinion (counts as *available*)
      * ``None``              cannot be evaluated (excluded from the cap)
    """

    id: str
    label: str
    requires_oi: bool

    def evaluate(self, ctx: RuleContext) -> RuleVote | None: ...


class RuleRegistry:
    """Ordered, immutable-by-convention registry of rules."""

    def __init__(self, rules: Sequence[Rule] = ()) -> None:
        self._rules: dict[str, Rule] = {}
        for rule in rules:
            self.register(rule)

    def register(self, rule: Rule) -> None:
        if not rule.id:
            raise ValueError("rule.id must be a non-empty string")
        if rule.id in self._rules:
            raise ValueError(f"duplicate rule id: {rule.id!r}")
        self._rules[rule.id] = rule

    def get(self, rule_id: str) -> Rule:
        try:
            return self._rules[rule_id]
        except KeyError as exc:
            raise KeyError(f"unknown rule {rule_id!r}; registered: {list(self._rules)}") from exc

    def __contains__(self, rule_id: object) -> bool:
        return rule_id in self._rules

    def __iter__(self) -> Iterator[Rule]:
        return iter(self._rules.values())

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._rules)

    def enabled(self, params: StrategyParams) -> tuple[tuple[Rule, RuleSettings], ...]:
        """Rules enabled by ``params``, in registry (reason-text) order."""
        selected: list[tuple[Rule, RuleSettings]] = []
        for rule_id, rule in self._rules.items():
            settings = params.rules.get(rule_id)
            if settings is None or not settings.enabled:
                continue
            selected.append((rule, settings))
        return tuple(selected)


def build_default_registry() -> RuleRegistry:
    """The five prototype rules, registered in their historical order."""
    from .rules import RULES

    return RuleRegistry(RULES)
