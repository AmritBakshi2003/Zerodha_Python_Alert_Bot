"""Per-symbol parameter overrides.

Two instruments rarely want identical thresholds (a NIFTY future and a mid-cap
equity behave differently), so overrides are data, not code::

    defaults:
      min_agree: 4
    symbols:
      NSE:INFY:
        rsi_period: 21
        cooldown_minutes: 30
      NFO:NIFTY-FUT:
        min_agree: 3

Overrides are re-validated through ``StrategyParams`` so an override can never
create an invalid combination that the base config would have rejected.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from ..core.errors import ConfigError
from .settings import StrategyParams

log = logging.getLogger(__name__)


class SymbolOverrides:
    """Resolved ``symbol -> StrategyParams`` with alias inheritance."""

    def __init__(
        self,
        base: StrategyParams,
        per_symbol: Mapping[str, StrategyParams] | None = None,
    ) -> None:
        self.base = base
        self._per_symbol = {k.upper(): v for k, v in (per_symbol or {}).items()}

    def __len__(self) -> int:
        return len(self._per_symbol)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._per_symbol)

    @classmethod
    def empty(cls, base: StrategyParams) -> SymbolOverrides:
        return cls(base)

    @classmethod
    def load(cls, path: str | Path | None, base: StrategyParams) -> SymbolOverrides:
        if not path:
            return cls(base)
        file_path = Path(path)
        if not file_path.exists():
            raise ConfigError(
                f"OVERRIDES_FILE={file_path} does not exist (leave it empty to use one config)"
            )
        try:
            raw: Any = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"overrides file {file_path} is not valid YAML: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ConfigError(f"overrides file {file_path} must be a mapping")

        known = {"defaults", "symbols"}
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(
                f"overrides file {file_path}: unknown top-level key(s) {sorted(unknown)}; "
                f"expected {sorted(known)}"
            )

        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, Mapping):
            raise ConfigError(f"overrides file {file_path}: 'defaults' must be a mapping")
        effective_base = cls._apply(base, defaults, f"{file_path}: defaults")

        symbols_raw = raw.get("symbols") or {}
        if not isinstance(symbols_raw, Mapping):
            raise ConfigError(f"overrides file {file_path}: 'symbols' must be a mapping")

        per_symbol: dict[str, StrategyParams] = {}
        for symbol, overrides in symbols_raw.items():
            if not isinstance(overrides, Mapping):
                raise ConfigError(
                    f"overrides file {file_path}: entry for {symbol!r} must be a mapping"
                )
            per_symbol[str(symbol)] = cls._apply(
                effective_base, overrides, f"{file_path}: symbols.{symbol}"
            )
        log.debug("loaded %d per-symbol override(s) from %s", len(per_symbol), file_path)
        return cls(effective_base, per_symbol)

    @staticmethod
    def _apply(base: StrategyParams, overrides: Mapping[str, Any], where: str) -> StrategyParams:
        try:
            return base.with_overrides(overrides)
        except ConfigError:
            raise
        except Exception as exc:  # pydantic ValidationError
            raise ConfigError(f"{where}: {exc}") from exc

    def for_symbol(self, symbol: str, aliases: Mapping[str, str] | None = None) -> StrategyParams:
        """Parameters for ``symbol``, honouring an alias entry if one matches.

        ``aliases`` maps a configured alias (e.g. ``NFO:NIFTY-FUT``) to the
        resolved symbol (``NFO:NIFTY26NOVFUT``) so an override written against
        the alias keeps working when the contract rolls.
        """
        key = symbol.upper()
        if key in self._per_symbol:
            return self._per_symbol[key]
        if aliases:
            for alias, resolved in aliases.items():
                if resolved.upper() == key and alias.upper() in self._per_symbol:
                    return self._per_symbol[alias.upper()]
        return self.base
