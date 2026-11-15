"""Only the tiny read-only slice of Kite Connect the dashboard needs is used."""

from __future__ import annotations

import re
from pathlib import Path

import kite_alerts

UI = Path(kite_alerts.__file__).parent / "ui"
STATIC = UI / "static"

#: The complete SDK surface the connect flow is allowed to call.
ALLOWED_CHAINED = {
    "login_url",
    "generate_session",
    "profile",
    "ltp",
    "invalidate_access_token",
}

FORBIDDEN_METHODS = (
    "place_order",
    "modify_order",
    "cancel_order",
    "order_history",
    "order_trades",
    "place_gtt",
    "modify_gtt",
    "cancel_gtt",
    "exit_order",
    "exit_positions",
    "convert_position",
    "basket_order",
    "place_basket",
    "retrieve_order",
    "margins",
    "holdings",
    "positions",
    "orders",
    "trades",
    "quote",
    "ohlc",
)

#: All allowed KiteConnect and KiteTicker SDK calls across the UI package.
UI_ALLOWED_KITE_METHODS = {
    "login_url",
    "generate_session",
    "profile",
    "ltp",
    "invalidate_access_token",
    "set_access_token",
    "historical_data",
    "instruments",
    "connect",
    "subscribe",
    "set_mode",
    "unsubscribe",
    "close",
    "stop",
    # our own KiteConnectService (context.kite), not the SDK client
    "start",
    "finish",
    "session",
    "disconnect",
    "data_sources",
    "pending_login_url",
    "cancel_pending",
}


def connect_source() -> str:
    return (UI / "kite_connect.py").read_text(encoding="utf-8")


def test_the_connect_module_only_chains_allowed_calls() -> None:
    source = connect_source()
    chained = set(re.findall(r"self\._client\([^)]*\)\.([a-z_]+)\(", source))
    assert chained == ALLOWED_CHAINED, chained


def test_the_only_other_sdk_call_sets_a_token() -> None:
    source = connect_source()
    calls = re.findall(r"\bclient\.([a-z_]+)\(", source)
    assert calls == ["set_access_token"], calls


def test_the_sdk_is_imported_exactly_once() -> None:
    source = connect_source()
    assert source.count("from kiteconnect import") == 1
    assert "default_kite_factory" in source


def test_the_probe_is_one_index_ltp_call() -> None:
    source = connect_source()
    assert source.count('ltp(["NSE:NIFTY 50"])') == 1
    assert "historical_data" not in source


def test_no_forbidden_kite_method_anywhere_in_the_ui() -> None:
    offenders: list[str] = []
    for path in sorted(UI.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for method in FORBIDDEN_METHODS:
            if f".{method}(" in text:
                offenders.append(f"{path.name}: {method}")
    assert not offenders, f"forbidden Kite call(s): {offenders}"


def test_the_ui_never_imports_order_helpers() -> None:
    for path in sorted(UI.rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        assert "place_order" not in text
        assert "kite.orders" not in text
        assert "/orders/" not in text


def test_frontend_never_calls_kite_or_telegram_directly() -> None:
    offenders: list[str] = []
    for path in sorted(STATIC.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for host in ("api.telegram.org", "developers.kite.trade", "kiteconnect"):
            if host in text:
                offenders.append(f"{path.name}: {host}")
    assert not offenders, offenders


def test_frontend_fetches_only_same_origin_paths() -> None:
    for path in sorted(STATIC.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"fetch\(\s*([^,\n]+)", text):
            target = match.group(1).strip()
            assert target.startswith('"/') or target.startswith("path"), (
                f"{path.name} fetches {target}"
            )


def test_ui_only_calls_allowlisted_kite_methods() -> None:
    pattern = re.compile(r"\b(?:kite|ticker|self\._ticker|self\._client\([^)]*\))\.([a-z_]+)\(")
    unexpected: set[str] = set()
    for path in sorted(UI.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            method = match.group(1)
            if not method.startswith("_") and method not in UI_ALLOWED_KITE_METHODS:
                unexpected.add(f"{path.name}: {method}")
    assert not unexpected, f"unexpected Kite method(s) in UI: {unexpected}"
