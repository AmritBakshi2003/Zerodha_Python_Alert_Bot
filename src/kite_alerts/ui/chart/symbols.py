"""Symbol mapping for the free sources.

The delayed NSE source is Yahoo Finance, which uses ``.NS`` tickers and its own
index symbols. NFO futures/options have no free delayed equivalent here, so they
are shown as unsupported instead of being silently remapped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .types import SymbolInfo

NSE_INDEX_MAP: dict[str, tuple[str, str]] = {
    "NSE:NIFTY 50": ("^NSEI", "NIFTY 50"),
    "NSE:NIFTY": ("^NSEI", "NIFTY 50"),
    "NSE:BANK NIFTY": ("^NSEBANK", "BANK NIFTY"),
    "NSE:BANKNIFTY": ("^NSEBANK", "BANK NIFTY"),
    "NSE:NIFTY BANK": ("^NSEBANK", "BANK NIFTY"),
}

DEFAULT_NSE_SYMBOLS: tuple[str, ...] = ("NSE:RELIANCE", "NSE:NIFTY 50")

CRYPTO_SYMBOLS: tuple[SymbolInfo, ...] = (
    SymbolInfo("BTC", "BTC / USD", "XBTUSD"),
    SymbolInfo("ETH", "ETH / USD", "ETHUSD"),
)

DEMO_SYMBOL = SymbolInfo("DEMO:FUT", "DEMO:FUT", "DEMO:FUT")

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9&._-]{0,39}$")
_SYMBOL_RE = re.compile(r"^(?:[A-Z0-9]{2,8}:[A-Z0-9][A-Z0-9&._ -]{0,39}|[A-Z0-9]{2,10})$")


def resolve_nse_symbol(raw: str) -> SymbolInfo:
    """Map one configured symbol to a Yahoo ticker, or explain why it cannot be."""
    symbol = " ".join(str(raw).strip().upper().split())
    if symbol in NSE_INDEX_MAP:
        provider, label = NSE_INDEX_MAP[symbol]
        return SymbolInfo(symbol, label, provider)
    if symbol.startswith("NFO:"):
        return SymbolInfo(
            symbol,
            symbol,
            "",
            supported=False,
            reason=(
                "NFO futures and options are not available from the delayed NSE "
                "source. Choose an NSE equity or index, or use Kite live later."
            ),
        )
    if not symbol.startswith("NSE:"):
        return SymbolInfo(
            symbol,
            symbol,
            "",
            supported=False,
            reason=(
                "The delayed NSE source only maps NSE equities (NSE:RELIANCE) and the "
                "NIFTY 50 / BANK NIFTY indices."
            ),
        )
    ticker = symbol.split(":", 1)[1]
    if not _TICKER_RE.match(ticker):
        return SymbolInfo(
            symbol,
            symbol,
            "",
            supported=False,
            reason="That NSE ticker contains characters the delayed source cannot map.",
        )
    return SymbolInfo(symbol, ticker, f"{ticker}.NS")


def delayed_symbols(configured: Iterable[str]) -> tuple[SymbolInfo, ...]:
    """Configured symbols plus safe defaults, preserving order and dropping duplicates."""
    seen: set[str] = set()
    result: list[SymbolInfo] = []
    source = list(configured) or list(DEFAULT_NSE_SYMBOLS)
    for raw in source:
        info = resolve_nse_symbol(raw)
        if info.symbol in seen:
            continue
        seen.add(info.symbol)
        result.append(info)
    return tuple(result)


def crypto_symbols() -> tuple[SymbolInfo, ...]:
    return CRYPTO_SYMBOLS


def is_safe_symbol(symbol: str) -> bool:
    return bool(_SYMBOL_RE.match(str(symbol).strip().upper()))


def lookup_nse(configured: Iterable[str], symbol: str) -> SymbolInfo:
    normalized = " ".join(str(symbol).strip().upper().split())
    for info in delayed_symbols(configured):
        if info.symbol == normalized:
            return info
    return resolve_nse_symbol(normalized)
