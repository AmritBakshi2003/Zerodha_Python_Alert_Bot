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
        # our own KiteConnectService (context.kite), not the SDK client
        "start",
        "finish",
        "session",
        "disconnect",
        "data_sources",
        "pending_login_url",
        "cancel_pending",
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


def test_c2_modules_are_scanned_for_order_apis() -> None:
    """The C2 pages (settings, connect, practice) ship more JS than C1 did."""
    suffixes = {path.name for path in ui_asset_files()}
    assert {"settings.js", "connect.js", "practice.js", "dom.js"} <= suffixes


_INJECTION_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


def test_ui_never_uses_a_markup_injection_sink() -> None:
    offenders: list[str] = []
    for path in ui_asset_files():
        if "vendor" in path.parts:
            continue  # third-party chart library, licensed and unmodified
        if path.suffix.lower() not in {".js", ".mjs", ".html"}:
            continue
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{path.name}: {sink}" for sink in _INJECTION_SINKS if sink in text)
    assert not offenders, f"unescaped DOM sink(s): {offenders}"


def test_notification_hosts_only_live_on_the_server() -> None:
    """The browser talks to this dashboard, never to Telegram or Slack."""
    hosts = ("api.telegram.org", "hooks.slack.com")
    offenders: list[str] = []
    for path in ui_asset_files():
        if path.suffix.lower() not in {".js", ".mjs", ".html", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{path.name}: {host}" for host in hosts if host in text)
    assert not offenders, offenders


def test_no_cdn_in_any_ui_asset() -> None:
    forbidden = ("cdn.", "unpkg", "jsdelivr", "googleapis", "fonts.gstatic")
    offenders: list[str] = []
    for path in ui_asset_files():
        if path.suffix.lower() not in {".js", ".mjs", ".html", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{path.name}: {host}" for host in forbidden if host in text)
    assert not offenders, f"external asset host(s) referenced: {offenders}"


def test_c3_chart_package_and_assets_are_scanned() -> None:
    """The chart workstation is Python plus JS/HTML, and is in the same guard."""
    root = Path(kite_alerts.__file__).parent / "ui"
    chart_files = sorted((root / "chart").glob("*.py"))
    assert {"sources.py", "service.py", "transforms.py"} <= {path.name for path in chart_files}
    names = {path.name for path in ui_asset_files()}
    assert {"chart.js", "index.html", "styles.css"} <= names
    assert any(path.suffix == ".py" for path in package_files())


def test_c4_live_feed_and_simulator_are_scanned_and_alerts_isolated() -> None:
    """Live feed and simulator must never touch alert engine, store, or notifier."""
    root = Path(kite_alerts.__file__).parent / "ui"
    ui_files = {p.name for p in root.glob("*.py")}
    assert {"live_feed.py", "simulator.py"} <= ui_files
    for mod_name in ("live_feed.py", "simulator.py"):
        text = (root / mod_name).read_text(encoding="utf-8").lower()
        assert "from ..engine" not in text
        assert "from .engine" not in text
        assert "alertstore" not in text
        assert "alert_store" not in text
        assert "notifiers" not in text
        assert "send_alert" not in text
