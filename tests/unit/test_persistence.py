"""Durable alert state: dedup, cooldown source, engine state, audit rows."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from kite_alerts.core.models import Alert, Side
from kite_alerts.persistence.store import InMemoryAlertStore, SqliteAlertStore


def make_alert(symbol: str = "NSE:TEST", side: Side = Side.BUY, minute: int = 0) -> Alert:
    return Alert(
        symbol=symbol,
        side=side,
        ts=pd.Timestamp("2026-11-16 10:00", tz="Asia/Kolkata") + pd.Timedelta(minutes=minute),
        price=100.5,
        reasons=("EMA9 crossed above EMA21",),
        rules_available=5,
        params={"min_agree": 4},
        rules_used=("ema_cross",),
    )


class TestSqliteStore:
    def test_record_and_dedup(self, sqlite_store: SqliteAlertStore) -> None:
        alert = make_alert()
        assert sqlite_store.record_alert(alert) is True
        assert sqlite_store.record_alert(alert) is False
        assert sqlite_store.count_alerts() == 1

    def test_last_alert_drives_cooldown(self, sqlite_store: SqliteAlertStore) -> None:
        sqlite_store.record_alert(make_alert(minute=0))
        sqlite_store.record_alert(make_alert(side=Side.SELL, minute=30))
        side, ts = sqlite_store.last_alert("NSE:TEST")
        assert side == "SELL"
        assert ts is not None
        assert ts.minute == 30

    def test_unknown_symbol_has_no_history(self, sqlite_store: SqliteAlertStore) -> None:
        assert sqlite_store.last_alert("NSE:NONE") == (None, None)

    def test_engine_state_roundtrip(self, sqlite_store: SqliteAlertStore) -> None:
        when = pd.Timestamp("2026-11-16 11:30", tz="Asia/Kolkata")
        assert sqlite_store.last_candle("NSE:TEST") is None
        sqlite_store.set_last_candle("NSE:TEST", when)
        assert sqlite_store.last_candle("NSE:TEST") == when

    def test_audit_row_contains_reasons_and_params(self, sqlite_store: SqliteAlertStore) -> None:
        sqlite_store.record_alert(make_alert())
        row = sqlite_store.audit_tail(1)[0]
        assert json.loads(row["reasons"]) == ["EMA9 crossed above EMA21"]
        assert json.loads(row["params"]) == {"min_agree": 4}
        assert row["rules_available"] == 5

    def test_mark_delivered(self, sqlite_store: SqliteAlertStore) -> None:
        alert = make_alert()
        sqlite_store.record_alert(alert)
        sqlite_store.mark_delivered(alert.dedup_key, ["console"], True)
        row = sqlite_store.audit_tail(1)[0]
        assert row["delivered"] == 1
        assert row["channels"] == "console"

    def test_state_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "alerts.db"
        with SqliteAlertStore(path) as store:
            store.record_alert(make_alert())
            store.set_last_candle("NSE:TEST", pd.Timestamp("2026-11-16 10:00", tz="Asia/Kolkata"))
        with SqliteAlertStore(path) as reopened:
            assert reopened.count_alerts() == 1
            assert reopened.last_candle("NSE:TEST") is not None

    def test_prune_removes_old_rows(self, sqlite_store: SqliteAlertStore) -> None:
        old = Alert(
            symbol="NSE:OLD",
            side=Side.BUY,
            ts=pd.Timestamp("2020-01-01 10:00", tz="Asia/Kolkata"),
            price=1.0,
        )
        sqlite_store.record_alert(old)
        sqlite_store.record_alert(make_alert())
        removed = sqlite_store.prune(retention_days=30)
        assert removed == 1
        assert sqlite_store.count_alerts() == 1


class TestInMemoryStore:
    def test_dedup_and_cooldown(self) -> None:
        store = InMemoryAlertStore()
        alert = make_alert()
        assert store.record_alert(alert) is True
        assert store.record_alert(alert) is False
        assert store.last_alert("NSE:TEST")[0] == "BUY"
        assert store.last_alert("NSE:OTHER") == (None, None)

    def test_engine_state(self) -> None:
        store = InMemoryAlertStore()
        when = pd.Timestamp("2026-11-16 12:00", tz="Asia/Kolkata")
        store.set_last_candle("NSE:TEST", when)
        assert store.last_candle("NSE:TEST") == when

    def test_records_delivery(self) -> None:
        store = InMemoryAlertStore()
        alert = make_alert()
        store.record_alert(alert)
        store.mark_delivered(alert.dedup_key, ["console"], True)
        assert store.delivered[alert.dedup_key] is True

    def test_alerts_are_exposed_read_only(self) -> None:
        store = InMemoryAlertStore()
        store.record_alert(make_alert())
        assert isinstance(store.alerts, tuple)


def test_sqlite_store_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "alerts.db"
    with SqliteAlertStore(path) as store:
        store.record_alert(make_alert())
    assert path.exists()


def test_sqlite_store_context_manager_closes(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    store = SqliteAlertStore(path)
    store.close()
    with pytest.raises(Exception):
        store.record_alert(make_alert())
