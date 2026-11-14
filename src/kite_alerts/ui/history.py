"""Read-only view of the bot's SQLite alert store.

The dashboard must never be able to damage the live bot's state. The database is
therefore opened per query with SQLite's ``mode=ro`` URI flag *and*
``PRAGMA query_only=ON``, so an accidental write fails at the SQLite level rather
than being prevented only by application code.

The store is optional: a fresh install has no database yet, which is an empty
state, not an error.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.clock import Clock, SystemClock, to_ist
from ..core.models import Side

log = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
EMPTY_MESSAGE = "No alerts yet. Try the demo player."
MISSING_TABLE_MESSAGE = (
    "This database does not have an alerts table yet. It fills up once the bot fires "
    "its first alert."
)
UNREADABLE_MESSAGE = (
    "The dashboard could not read the alert history file. It may be in use or damaged — "
    "check the path shown by `kite-alerts config-check`."
)


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _reasons(raw: object) -> list[str]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, (str, int, float))]


def _hhmm(ts: str) -> str:
    """``HH:MM`` from a stored ISO timestamp (kept offset-aware when written)."""
    return ts[11:16] if len(ts) >= 16 else ts


def alert_item(row: sqlite3.Row) -> dict[str, Any]:
    """One alert, shaped for the browser (no credential-shaped fields exist here)."""
    reasons = _reasons(row["reasons"])
    side = str(row["side"])
    price = float(row["price"])
    ts = _as_text(row["ts"])
    icon = Side.BUY.icon if side == Side.BUY.value else Side.SELL.icon
    channels = [name for name in str(row["channels"] or "").split(",") if name]
    return {
        "id": int(row["id"]),
        "ts": ts,
        "time": _hhmm(ts),
        "symbol": str(row["symbol"]),
        "side": side,
        "side_icon": icon,
        "price": price,
        "reasons": reasons,
        "message": f"{icon} {side} {row['symbol']} @ {price:.2f} [{_hhmm(ts)}] — "
        + "; ".join(reasons),
        "delivered": bool(row["delivered"]),
        "channels": channels,
    }


@dataclass(frozen=True, slots=True)
class AlertPage:
    """One page of alert history plus everything the UI needs for an empty state."""

    items: tuple[dict[str, Any], ...] = ()
    total: int = 0
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    pages: int = 1
    message: str | None = None
    symbols: tuple[str, ...] = ()
    available: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": list(self.items),
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "pages": self.pages,
            "message": self.message,
            "symbols": list(self.symbols),
            "available": self.available,
        }


@dataclass
class AlertHistoryReader:
    """Read-only queries against ``DB_PATH`` (or ``--db``)."""

    path: Path
    clock: Clock = field(default_factory=SystemClock)

    @property
    def db_uri(self) -> str:
        """SQLite URI that is guaranteed read-only."""
        return f"{self.path.resolve().as_uri()}?mode=ro"

    def open_read_only(self) -> sqlite3.Connection | None:
        """A read-only connection, or ``None`` when the database does not exist yet."""
        if not self.path.exists():
            return None
        connection = sqlite3.connect(self.db_uri, uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    # ------------------------------------------------------------------ queries
    def read_page(
        self,
        *,
        symbol: str | None = None,
        side: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> AlertPage:
        page = max(1, int(page))
        page_size = min(max(1, int(page_size)), MAX_PAGE_SIZE)
        symbol = (symbol or "").strip().upper() or None
        side = (side or "").strip().upper() or None
        if side is not None and side not in (Side.BUY.value, Side.SELL.value):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")

        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if side:
            clauses.append("side = ?")
            params.append(side)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        try:
            connection = self.open_read_only()
        except sqlite3.Error:
            log.warning("could not open the alert store read-only", exc_info=True)
            return AlertPage(
                page=page, page_size=page_size, message=UNREADABLE_MESSAGE, available=False
            )
        if connection is None:
            return AlertPage(page=page, page_size=page_size, message=EMPTY_MESSAGE)
        try:
            total = int(
                connection.execute(f"SELECT COUNT(*) AS n FROM alerts{where}", params).fetchone()[
                    "n"
                ]
            )
            rows = connection.execute(
                "SELECT * FROM alerts" + where + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            symbol_rows = connection.execute(
                "SELECT DISTINCT symbol FROM alerts ORDER BY symbol"
            ).fetchall()
        except sqlite3.OperationalError:
            log.warning("alert store has no usable alerts table", exc_info=True)
            return AlertPage(
                page=page, page_size=page_size, message=MISSING_TABLE_MESSAGE, available=False
            )
        except sqlite3.Error:
            log.warning("could not read the alert store", exc_info=True)
            return AlertPage(
                page=page, page_size=page_size, message=UNREADABLE_MESSAGE, available=False
            )
        finally:
            connection.close()

        pages = max(1, (total + page_size - 1) // page_size)
        items = tuple(alert_item(row) for row in rows)
        return AlertPage(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            pages=pages,
            message=None if items else EMPTY_MESSAGE,
            symbols=tuple(str(row["symbol"]) for row in symbol_rows),
        )

    def recent(self, limit: int = 20) -> dict[str, Any]:
        """Newest alerts, for the live-updating dashboard feed."""
        limit = min(max(1, int(limit)), MAX_PAGE_SIZE)
        try:
            connection = self.open_read_only()
        except sqlite3.Error:
            return {"items": [], "message": UNREADABLE_MESSAGE, "available": False}
        if connection is None:
            return {"items": [], "message": EMPTY_MESSAGE, "available": True}
        try:
            rows = connection.execute(
                "SELECT * FROM alerts ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        except sqlite3.OperationalError:
            return {"items": [], "message": MISSING_TABLE_MESSAGE, "available": False}
        except sqlite3.Error:
            return {"items": [], "message": UNREADABLE_MESSAGE, "available": False}
        finally:
            connection.close()
        items = [alert_item(row) for row in rows]
        return {"items": items, "message": None if items else EMPTY_MESSAGE, "available": True}

    def today_counts(self) -> dict[str, Any]:
        """Buy/sell counters for today's IST date (stored timestamps are offset-aware)."""
        today = to_ist(self.clock.now()).date().isoformat()
        counts: dict[str, Any] = {
            "date": today,
            "buys": 0,
            "sells": 0,
            "total": 0,
            "available": True,
        }
        try:
            connection = self.open_read_only()
        except sqlite3.Error:
            counts["available"] = False
            return counts
        if connection is None:
            return counts
        try:
            rows = connection.execute(
                "SELECT side, COUNT(*) AS n FROM alerts WHERE substr(ts, 1, 10) = ? GROUP BY side",
                (today,),
            ).fetchall()
        except sqlite3.Error:
            counts["available"] = False
            return counts
        finally:
            connection.close()
        for row in rows:
            if row["side"] == Side.BUY.value:
                counts["buys"] = int(row["n"])
            elif row["side"] == Side.SELL.value:
                counts["sells"] = int(row["n"])
        counts["total"] = counts["buys"] + counts["sells"]
        return counts
