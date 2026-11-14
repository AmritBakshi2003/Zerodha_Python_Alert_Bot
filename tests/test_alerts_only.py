"""Hard constraint #1: the source tree must never reference an order API.

This test scans every Python file in the installed package (``src/kite_alerts``)
for Kite Connect's order-side method names. If a future change adds one - even
behind a feature flag - CI fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import kite_alerts

#: Order-side SDK surface (place/modify/cancel orders and GTTs, exits, baskets,
#: position conversion and the order book). Assembled at runtime so this module
#: does not itself contain the forbidden literals.
_FORBIDDEN = tuple(
    "_".join(parts)
    for parts in (
        ("place", "order"),
        ("modify", "order"),
        ("cancel", "order"),
        ("place", "gtt"),
        ("modify", "gtt"),
        ("cancel", "gtt"),
        ("exit", "order"),
        ("exit", "positions"),
        ("convert", "position"),
        ("basket", "order"),
        ("retrieve", "order"),
        ("order", "history"),
        ("order", "trades"),
        ("place", "basket"),
    )
)

_ORDER_ENDPOINT_HINTS = ("/orders/", "order.place", "kite.orders")


def package_files() -> list[Path]:
    root = Path(kite_alerts.__file__).parent
    return sorted(root.rglob("*.py"))


#: Everything the dashboard can ship to a browser. The guard must see it too: an
#: order API referenced from JavaScript is just as forbidden as one in Python.
_UI_ASSET_SUFFIXES = {".py", ".js", ".mjs", ".html", ".css", ".json"}


def ui_asset_files() -> list[Path]:
    root = Path(kite_alerts.__file__).parent / "ui"
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _UI_ASSET_SUFFIXES
    )


def test_package_has_files() -> None:
    assert len(package_files()) > 20


@pytest.mark.parametrize("name", _FORBIDDEN)
def test_no_order_method_reference(name: str) -> None:
    offenders = [
        str(path.relative_to(Path(kite_alerts.__file__).parent))
        for path in package_files()
        if name in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"order API {name!r} referenced in: {offenders}"


def test_ui_assets_are_scanned() -> None:
    """The dashboard's static files exist and are included in the order-API scan."""
    suffixes = {path.suffix.lower() for path in ui_asset_files()}
    assert {".js", ".html", ".css"} <= suffixes


@pytest.mark.parametrize("name", _FORBIDDEN)
def test_ui_never_references_an_order_api(name: str) -> None:
    offenders = [
        str(path.relative_to(Path(kite_alerts.__file__).parent))
        for path in ui_asset_files()
        if name in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"order API {name!r} referenced by the UI: {offenders}"


def test_no_order_endpoint_strings() -> None:
    offenders: list[str] = []
    for path in [*package_files(), *ui_asset_files()]:
        text = path.read_text(encoding="utf-8")
        for hint in _ORDER_ENDPOINT_HINTS:
            if hint in text:
                offenders.append(f"{path.name}: {hint}")
    assert not offenders, f"order endpoint referenced in: {offenders}"


def test_ui_declares_alerts_only() -> None:
    """The page the customer sees must state the hard constraint."""
    index = Path(kite_alerts.__file__).parent / "ui" / "static" / "index.html"
    text = index.read_text(encoding="utf-8").lower()
    assert "never places orders" in text


def test_only_market_data_kite_methods_are_used() -> None:
    """Every ``kite.<method>`` call must be a read-only market-data call."""
    allowed = {
        "historical_data",
        "instruments",
        "profile",
        "ltp",
        "quote",
        "ohlc",
        "set_access_token",
        "login_url",
        "generate_session",
        "instruments_data",
        "history_data",
        "history_calls",
        # our own credentials object (KiteCredentials), not the SDK client
        "require",
    }
    pattern = re.compile(r"\b(?:kite|self\._kite|active_kite)\.([a-z_]+)\(")
    found: set[str] = set()
    for path in package_files():
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            found.add(match.group(1))
    unexpected = {name for name in found if name not in allowed}
    assert not unexpected, f"unexpected Kite client method(s) used: {unexpected}"


def test_package_documents_alerts_only() -> None:
    docstring = (kite_alerts.__doc__ or "").lower()
    assert "alerts only" in docstring or "alert" in docstring
