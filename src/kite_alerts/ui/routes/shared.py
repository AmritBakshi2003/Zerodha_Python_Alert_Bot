"""Shared constants, helpers and request models for the dashboard routers.

The routers in this package are thin: they read from an injected
:class:`~kite_alerts.ui.app.UiContext` and keep the cross-cutting bits
(paths, response helpers, request models) here so no router repeats them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..demo_player import DemoTimeline

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import cycle
    from ..app import UiContext


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"
LICENSE_FILE = STATIC_DIR / "vendor" / "lightweight-charts" / "LICENSE"
NOTICE_FILE = STATIC_DIR / "vendor" / "lightweight-charts" / "NOTICE"

#: Shown when a deployment bundle omits the static assets (e.g. Vercel's
#: Python function bundle). Reading index.html at *import* time made every
#: request fail with FUNCTION_INVOCATION_FAILED when static/ was absent.
_INDEX_FALLBACK = (
    "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
    "<title>kite-alerts dashboard</title></head><body>"
    "<main><h1>kite-alerts dashboard</h1>"
    "<p>The dashboard assets are not present in this deployment. "
    "Run <code>kite-alerts ui</code> locally to use the dashboard.</p>"
    "</main></body></html>"
)


def load_index_html() -> str:
    """Return the dashboard HTML, or an honest fallback if it was not bundled."""
    try:
        return INDEX_FILE.read_text(encoding="utf-8")
    except OSError:
        return _INDEX_FALLBACK


MODE = (
    "Read-only dashboard with a practice replay, a guided Kite connection and settings. "
    "The live alert feed is started with `kite-alerts run`, not from here."
)

ALERTS_ONLY_STATEMENT = (
    "This tool only sends alerts. It never places, modifies or cancels orders, "
    "and it never touches your positions."
)

#: Exact attribution required by the vendored chart library (see its NOTICE).
ATTRIBUTION = {
    "name": "TradingView Lightweight Charts™",
    "version": "5.0.9",
    "license": "Apache-2.0",
    "notice": (
        "TradingView Lightweight Charts™\n"
        "Copyright (с) 2025 TradingView, Inc. https://www.tradingview.com/"
    ),
    "homepage": "https://www.tradingview.com/",
    "license_url": "/license",
    "notice_url": "/notice",
}

FIRST_DAY_CHECKLIST = (
    "Open Connect and create your own Kite Connect app at developers.kite.trade.",
    "Copy this dashboard's redirect URL into that app's settings.",
    "Paste your own API key and secret on Connect, then log in on Zerodha's page.",
    "On Settings, choose the instruments you watch and enable Telegram or Slack.",
    "Run `kite-alerts config-check` (or press Check settings) to confirm the file.",
    "Run `kite-alerts run` in its own window; the dashboard only watches it.",
    "Try the Practice page first: it needs no account and no internet.",
)

NO_SETTINGS_MESSAGE = (
    "Your settings could not be read. The practice replay still works, but connecting, "
    "saving and status need a valid .env."
)


class SettingsUpdate(BaseModel):
    """A partial settings update, keyed by environment variable name."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(default_factory=dict)


class ManualToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_token: str = ""
    state: str = ""


class WatchlistSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "nse_delayed"
    symbol: str = ""


def _ndjson(timeline: DemoTimeline) -> Iterator[str]:
    for event in timeline.events():
        yield json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n"


def _error(status: int, message: str, code: str = "error") -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message}, status_code=status)


def _practice_payload(timeline: DemoTimeline, version: str) -> dict[str, Any]:
    alerts = []
    for alert in timeline.alerts:
        alerts.append(
            {
                "side": alert.side,
                "ts": alert.ts,
                "time": alert.chart_time,
                "price": alert.price,
                "reasons": list(alert.reasons),
                "rules": [dict(rule) for rule in alert.rules],
                "rules_agreed": [
                    str(rule["label"]) for rule in alert.rules if rule["status"] == "agree"
                ],
            }
        )
    total = len(timeline.alerts)
    message = (
        f"With your saved settings, this practice day produces {total} alert(s): "
        f"{timeline.sells} SELL and {timeline.buys} BUY. Nothing is saved."
    )
    return {
        "ok": True,
        "demo": True,
        "label": "DEMO",
        "version": version,
        "symbol": timeline.symbol,
        "counts": {
            "buys": timeline.buys,
            "sells": timeline.sells,
            "total": total,
        },
        "alerts": alerts,
        "message": message,
    }


def _resolved_paths(context: UiContext) -> dict[str, Any]:
    """Absolute paths the dashboard actually uses (F14)."""
    settings = context.settings
    history_path = getattr(context.history, "path", None)
    warnings: list[str] = []
    if (
        settings is not None
        and history_path is not None
        and Path(history_path) != Path(settings.db_path)
    ):
        warnings.append(
            f"Dashboard reads {history_path} but .env configures {settings.db_path}; "
            "they differ (check --db / env_file)."
        )
    return {
        "env_file": str(context.env_file),
        "history_db": str(history_path) if history_path is not None else None,
        "settings_db": str(settings.db_path) if settings is not None else None,
        "log_dir": str(settings.observability.log_dir) if settings is not None else None,
        "cache_dir": str(settings.cache_dir) if settings is not None else None,
        "watchlist": str(context.env_file.parent / "data" / "chart_watchlist.json"),
        "warnings": warnings,
    }


__all__ = [
    "ALERTS_ONLY_STATEMENT",
    "ATTRIBUTION",
    "FIRST_DAY_CHECKLIST",
    "INDEX_FILE",
    "LICENSE_FILE",
    "MODE",
    "NOTICE_FILE",
    "NO_SETTINGS_MESSAGE",
    "STATIC_DIR",
    "_INDEX_FALLBACK",
    "SettingsUpdate",
    "_error",
    "_ndjson",
    "_practice_payload",
    "_resolved_paths",
    "load_index_html",
]
