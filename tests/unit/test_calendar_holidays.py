"""F3: holidays must actually switch the bot off, and never do it silently.

``config/holidays.yaml`` shipped as ``holidays: []`` and nothing warned about it,
so ``describe()`` could never return ``"holiday"`` and a market holiday looked
exactly like a stalled feed.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import logging
import sys
from pathlib import Path

import pytest

from kite_alerts.config.settings import StrategyParams
from kite_alerts.core import calendar as cal
from kite_alerts.core.clock import IST
from kite_alerts.core.errors import ConfigError
from kite_alerts.core.models import Candle
from kite_alerts.engine.engine import Engine
from kite_alerts.persistence.store import InMemoryAlertStore

REPO_ROOT = Path(__file__).resolve().parents[2]
MONDAY = dt.date(2026, 11, 16)


def _candle(moment: dt.datetime) -> Candle:
    return Candle(
        symbol="NSE:TEST",
        time=moment,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000,
        oi=0.0,
    )


def _at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), IST)


class _NullSink:
    def submit(self, alert: object) -> None:  # pragma: no cover - never called
        raise AssertionError("a holiday must never produce an alert")


class TestHolidayBehaviour:
    def test_configured_holiday_is_reported_and_blocks_trading(self, tmp_path: Path) -> None:
        path = tmp_path / "holidays.yaml"
        path.write_text("holidays:\n  - 2026-11-16\n", encoding="utf-8")

        calendar = cal.calendar_from_config(
            open_value="09:15", close_value="15:30", holiday_file=path, today=MONDAY
        )

        assert calendar.is_holiday(MONDAY)
        assert calendar.describe(_at(MONDAY, 10, 0)) == "holiday"
        assert calendar.in_session(_at(MONDAY, 10, 0)) is False

    def test_holiday_produces_no_alert(self, tmp_path: Path) -> None:
        path = tmp_path / "holidays.yaml"
        path.write_text("holidays:\n  - 2026-11-16\n", encoding="utf-8")
        calendar = cal.calendar_from_config(
            open_value="09:15", close_value="15:30", holiday_file=path, today=MONDAY
        )
        engine = Engine(
            params_for=lambda _symbol: StrategyParams(),
            store=InMemoryAlertStore(),
            sink=_NullSink(),
            calendar=calendar,
        )

        assert engine.on_candle(_candle(_at(MONDAY, 10, 0))) is None


class TestHolidayWarnings:
    def test_empty_file_warns_once_naming_file_and_year(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "holidays.yaml"
        path.write_text("holidays: []\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="kite_alerts.core.calendar"):
            cal.calendar_from_config(
                open_value="09:15", close_value="15:30", holiday_file=path, today=MONDAY
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "holidays.yaml" in message
        assert "2026" in message

    def test_stale_list_warns_about_the_current_year(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "holidays.yaml"
        path.write_text("holidays:\n  - 2024-01-26\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="kite_alerts.core.calendar"):
            cal.calendar_from_config(
                open_value="09:15", close_value="15:30", holiday_file=path, today=MONDAY
            )

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "2026" in warnings[0]

    def test_a_current_year_list_is_quiet(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "holidays.yaml"
        path.write_text("holidays:\n  - 2026-11-16\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="kite_alerts.core.calendar"):
            cal.calendar_from_config(
                open_value="09:15", close_value="15:30", holiday_file=path, today=MONDAY
            )

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


class TestSpecialSessionConfig:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "sessions.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_valid_special_session_loads(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "special_sessions:\n"
            "  - date: 2026-11-14\n"
            "    open: '18:15'\n"
            "    close: '19:15'\n"
            "    label: Muhurat trading\n",
        )

        sessions = cal.load_special_sessions(path)

        assert len(sessions) == 1
        assert sessions[0].day == dt.date(2026, 11, 14)
        assert sessions[0].open_time == dt.time(18, 15)
        assert sessions[0].label == "Muhurat trading"

    def test_duplicate_dates_are_rejected(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "special_sessions:\n"
            "  - date: 2026-11-14\n"
            "    open: '18:15'\n"
            "    close: '19:15'\n"
            "  - date: 2026-11-14\n"
            "    open: '20:00'\n"
            "    close: '21:00'\n",
        )

        with pytest.raises(ConfigError, match="duplicate"):
            cal.load_special_sessions(path)

    def test_open_after_close_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "special_sessions:\n  - date: 2026-11-14\n    open: '19:15'\n    close: '18:15'\n",
        )

        with pytest.raises(ConfigError, match="before"):
            cal.load_special_sessions(path)

    def test_missing_file_means_no_special_sessions(self, tmp_path: Path) -> None:
        assert cal.load_special_sessions(tmp_path / "absent.yaml") == ()


class TestRefreshHelper:
    """The refresh script is tested with fixture HTML: no network, no invented dates."""

    HTML = """
    <table>
      <tr><th>Sr</th><th>Date</th><th>Day</th><th>Description</th></tr>
      <tr><td>1</td><td>26-Jan-2026</td><td>Monday</td><td>Republic Day</td></tr>
      <tr><td>2</td><td>04-Mar-2026</td><td>Wednesday</td><td>Holi</td></tr>
      <tr><td>3</td><td>26-Jan-2027</td><td>Tuesday</td><td>Republic Day</td></tr>
    </table>
    """

    @pytest.fixture
    def refresh(self) -> object:
        cached = sys.modules.get("refresh_holidays")
        if cached is not None:
            return cached
        path = REPO_ROOT / "scripts" / "refresh_holidays.py"
        spec = importlib.util.spec_from_file_location("refresh_holidays", path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["refresh_holidays"] = module
        spec.loader.exec_module(module)
        return module

    def test_parses_only_the_requested_year(self, refresh: object) -> None:
        rows = refresh.parse_holidays_html(self.HTML, year=2026)  # type: ignore[attr-defined]
        assert [day for day, _name in rows] == [
            dt.date(2026, 1, 26),
            dt.date(2026, 3, 4),
        ]
        assert rows[0][1] == "Republic Day"

    def test_written_yaml_round_trips_through_the_loader(
        self, refresh: object, tmp_path: Path
    ) -> None:
        rows = refresh.parse_holidays_html(self.HTML, year=2026)  # type: ignore[attr-defined]
        out = tmp_path / "holidays.yaml"
        refresh.write_holidays(out, rows, year=2026)  # type: ignore[attr-defined]

        loaded = cal.load_holidays(out)
        assert loaded == {dt.date(2026, 1, 26), dt.date(2026, 3, 4)}
        assert "2026" in out.read_text(encoding="utf-8")

    def test_empty_html_is_an_error_not_an_empty_list(self, refresh: object) -> None:
        with pytest.raises(ValueError, match="no holidays"):
            refresh.parse_holidays_html("<html></html>", year=2026)  # type: ignore[attr-defined]
