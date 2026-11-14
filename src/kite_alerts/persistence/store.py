"""Durable state: fired alerts, cooldown, and the last processed candle.

Two production bugs this fixes:

* cooldown state lived in a dict, so a restart re-sent alerts;
* an alert had no durable record, so a failed notification vanished.

The ``alerts`` table is simultaneously the dedup ledger (``dedup_key`` is unique),
the cooldown source and the audit trail (params snapshot + reasons). SQLite is in
the standard library, runs in WAL mode and survives ``kill -9``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from ..core.clock import to_ist_ts
from ..core.models import Alert

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key        TEXT    NOT NULL UNIQUE,
    symbol           TEXT    NOT NULL,
    side             TEXT    NOT NULL,
    ts               TEXT    NOT NULL,
    price            REAL    NOT NULL,
    reasons          TEXT    NOT NULL,
    rules_used       TEXT    NOT NULL,
    rules_available  INTEGER NOT NULL,
    params           TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    delivered        INTEGER NOT NULL DEFAULT 0,
    channels         TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol_ts ON alerts(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);

CREATE TABLE IF NOT EXISTS engine_state (
    symbol          TEXT PRIMARY KEY,
    last_candle_ts  TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

SCHEMA_VERSION = "1"


class SqliteAlertStore:
    """Thread-safe SQLite store (WAL, one connection, serialised writes)."""

    def __init__(self, path: str | Path, *, retention_days: int = 365) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()
        self._retention_days = retention_days

    # ------------------------------------------------------------------ alerts
    def record_alert(self, alert: Alert) -> bool:
        """Insert the alert; return False when it is a duplicate (already sent)."""
        record = alert.audit_record()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO alerts
                    (dedup_key, symbol, side, ts, price, reasons, rules_used,
                     rules_available, params, created_at, delivered, channels)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '')
                """,
                (
                    alert.dedup_key,
                    alert.symbol,
                    alert.side.value,
                    alert.ts.isoformat(),
                    float(alert.price),
                    json.dumps(list(alert.reasons)),
                    json.dumps(list(alert.rules_used)),
                    int(alert.rules_available),
                    json.dumps(record["params"]),
                    pd.Timestamp.now(tz=alert.ts.tz).isoformat(),
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def mark_delivered(self, dedup_key: str, channels: list[str], ok: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE alerts SET delivered = ?, channels = ? WHERE dedup_key = ?",
                (1 if ok else 0, ",".join(channels), dedup_key),
            )
            self._conn.commit()

    def last_alert(self, symbol: str) -> tuple[str | None, pd.Timestamp | None]:
        with self._lock:
            row = self._conn.execute(
                "SELECT side, ts FROM alerts WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        if row is None:
            return None, None
        return str(row["side"]), to_ist_ts(row["ts"])

    def count_alerts(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()
        return int(row["n"]) if row else 0

    def audit_tail(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, retention_days: int | None = None) -> int:
        days = retention_days if retention_days is not None else self._retention_days
        cutoff = to_ist_ts(pd.Timestamp.now(tz="Asia/Kolkata")) - pd.Timedelta(days=days)
        with self._lock:
            cursor = self._conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff.isoformat(),))
            self._conn.commit()
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ engine state
    def last_candle(self, symbol: str) -> pd.Timestamp | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_candle_ts FROM engine_state WHERE symbol = ?", (symbol,)
            ).fetchone()
        return to_ist_ts(row["last_candle_ts"]) if row else None

    def set_last_candle(self, symbol: str, ts: pd.Timestamp) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO engine_state(symbol, last_candle_ts, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    last_candle_ts = excluded.last_candle_ts,
                    updated_at = excluded.updated_at
                """,
                (
                    symbol,
                    to_ist_ts(ts).isoformat(),
                    pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error as exc:  # pragma: no cover - shutdown path
                log.warning("error closing sqlite store: %s", exc)

    def __enter__(self) -> SqliteAlertStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class InMemoryAlertStore:
    """Same interface as the SQLite store, for the demo, replay and unit tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._alerts: list[Alert] = []
        self._keys: set[str] = set()
        self._last_candle: dict[str, pd.Timestamp] = {}
        self._delivered: dict[str, bool] = {}

    def record_alert(self, alert: Alert) -> bool:
        with self._lock:
            if alert.dedup_key in self._keys:
                return False
            self._keys.add(alert.dedup_key)
            self._alerts.append(alert)
            return True

    def mark_delivered(self, dedup_key: str, channels: list[str], ok: bool) -> None:
        with self._lock:
            self._delivered[dedup_key] = ok

    def last_alert(self, symbol: str) -> tuple[str | None, pd.Timestamp | None]:
        with self._lock:
            for alert in reversed(self._alerts):
                if alert.symbol == symbol:
                    return alert.side.value, alert.ts
        return None, None

    def last_candle(self, symbol: str) -> pd.Timestamp | None:
        with self._lock:
            return self._last_candle.get(symbol)

    def set_last_candle(self, symbol: str, ts: pd.Timestamp) -> None:
        with self._lock:
            self._last_candle[symbol] = to_ist_ts(ts)

    @property
    def alerts(self) -> tuple[Alert, ...]:
        with self._lock:
            return tuple(self._alerts)

    @property
    def delivered(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._delivered)

    def close(self) -> None:
        return None
