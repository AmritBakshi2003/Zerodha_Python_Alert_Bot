"""F25: the calendar cache must follow settings values, not object identity.

``id()`` is only unique among live objects: after a settings reload and GC, a new
``Settings`` can receive a freed address and inherit the old session window. The
cache is therefore keyed by the values that build the calendar, plus a stamp of
the two YAML files so editing ``holidays.yaml`` does not need a restart.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from kite_alerts.config.settings import SessionSettings, Settings
from kite_alerts.ui.chart.service import ChartService
from unit.ui_support import make_settings


def _chart() -> ChartService:
    return ChartService(settings_provider=lambda: None)


def _settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path / "alerts.db")


def _with_session(settings: Settings, **changes: str) -> Settings:
    session = settings.session.model_copy(update=changes)
    return settings.model_copy(update={"session": session})


def test_equal_settings_share_one_calendar(tmp_path: Path) -> None:
    """A reload creates a new object with the same values; it must reuse the calendar."""
    settings = _settings(tmp_path)
    chart = _chart()

    first = chart._calendar(settings)
    reloaded = settings.model_copy()

    assert reloaded is not settings
    assert chart._calendar(reloaded) is first


def test_changed_close_time_rebuilds_the_calendar(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    chart = _chart()

    original = chart._calendar(settings)
    later = chart._calendar(_with_session(settings, close_time="16:00", closing_end="16:30"))

    assert later is not original
    assert later.window.close_time == dt.time(16, 0)


def test_editing_the_holiday_file_invalidates_the_cache(tmp_path: Path) -> None:
    """The same Settings object, a changed file: the next request must see the holiday."""
    holiday_file = tmp_path / "holidays.yaml"
    holiday_file.write_text("holidays: []\n", encoding="utf-8")
    settings = _settings(tmp_path).model_copy(
        update={"session": SessionSettings(holiday_file=holiday_file)}
    )
    chart = _chart()

    first = chart._calendar(settings)
    assert dt.date(2026, 1, 26) not in first.holiday_dates

    holiday_file.write_text("holidays: [2026-01-26]\n", encoding="utf-8")
    second = chart._calendar(settings)

    assert second is not first
    assert dt.date(2026, 1, 26) in second.holiday_dates
