"""Time and session behaviour: IST correctness and holiday awareness."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from kite_alerts.core.calendar import SessionWindow, TradingCalendar, load_holidays
from kite_alerts.core.clock import (
    FrozenClock,
    floor_minute,
    format_hhmm,
    seconds_until_next_minute,
    to_ist,
    to_ist_ts,
)
from kite_alerts.core.errors import ConfigError


class TestIst:
    def test_naive_input_is_interpreted_as_ist(self) -> None:
        moment = to_ist(dt.datetime(2026, 11, 16, 9, 15))
        assert moment.utcoffset() == dt.timedelta(hours=5, minutes=30)
        assert moment.hour == 9
        assert moment.minute == 15

    def test_utc_input_is_converted(self) -> None:
        """The prototype's bug: a UTC host produced a 5h30m-shifted window."""
        utc = dt.datetime(2026, 11, 16, 4, 0, tzinfo=dt.timezone.utc)
        assert to_ist(utc).strftime("%H:%M") == "09:30"

    def test_dataframe_index_is_tz_aware_ist(self) -> None:
        ts = to_ist_ts("2026-11-16 09:15:00")
        assert str(ts.tz) in ("Asia/Kolkata", "IST")
        assert ts.strftime("%H:%M") == "09:15"

    def test_floor_minute(self) -> None:
        assert floor_minute(to_ist_ts("2026-11-16 09:15:59")) == to_ist_ts("2026-11-16 09:15")

    def test_seconds_until_next_minute_is_bounded(self) -> None:
        assert seconds_until_next_minute(to_ist_ts("2026-11-16 09:15:59")) == pytest.approx(1.0)
        assert seconds_until_next_minute(to_ist_ts("2026-11-16 09:15:00")) == pytest.approx(60.0)
        assert seconds_until_next_minute(to_ist_ts("2026-11-16 09:15:59.999")) >= 0.05

    def test_format_hhmm_uses_ist(self) -> None:
        utc = pd.Timestamp("2026-11-16 04:07", tz="UTC")
        assert format_hhmm(utc) == "09:37"

    def test_frozen_clock(self) -> None:
        clock = FrozenClock("2026-11-16 09:15:00")
        assert clock.now().strftime("%H:%M") == "09:15"
        clock.advance(90)
        assert clock.now().strftime("%H:%M") == "09:16"
        clock.set("2026-11-16 10:00:00")
        assert clock.now().hour == 10


class TestSessionWindow:
    def test_parse_and_minutes(self) -> None:
        window = SessionWindow.parse("09:15", "15:30")
        assert window.minutes == 375
        assert window.contains("2026-11-16 09:15")
        assert not window.contains("2026-11-16 09:14")
        assert window.contains("2026-11-16 15:30")
        assert not window.contains("2026-11-16 15:31")

    def test_invalid_window(self) -> None:
        with pytest.raises(ConfigError):
            SessionWindow.parse("15:30", "09:15")

    def test_bad_time_string(self) -> None:
        with pytest.raises(ConfigError, match="HH:MM"):
            SessionWindow.parse("nine", "15:30")


class TestCalendar:
    def test_weekday_session(self, calendar: TradingCalendar) -> None:
        assert calendar.is_trading_day(to_ist_ts("2026-11-16 10:00"))
        assert calendar.in_session(to_ist_ts("2026-11-16 10:00"))
        assert calendar.describe(to_ist_ts("2026-11-16 10:00")) == "open"

    def test_weekend(self, calendar: TradingCalendar) -> None:
        saturday = to_ist_ts("2026-11-14 10:00")
        assert calendar.is_weekend(saturday.date())
        assert not calendar.in_session(saturday)
        assert calendar.describe(saturday) == "weekend"

    def test_pre_open_and_post_close(self, calendar: TradingCalendar) -> None:
        assert calendar.describe(to_ist_ts("2026-11-16 09:05")) == "pre-open"
        # F4: 15:31 is the closing window; 01:40 is not "pre-open" any more.
        assert calendar.describe(to_ist_ts("2026-11-16 15:31")) == "closing"
        assert calendar.describe(to_ist_ts("2026-11-16 01:40")) == "closed"
        assert not calendar.in_session(to_ist_ts("2026-11-16 09:05"))

    def test_holiday(self, calendar: TradingCalendar, tmp_path) -> None:
        path = tmp_path / "h.yaml"
        path.write_text("holidays:\n  - 2026-11-16\n", encoding="utf-8")
        holiday_calendar = TradingCalendar(holidays=load_holidays(path))
        assert not holiday_calendar.in_session(to_ist_ts("2026-11-16 10:00"))
        assert holiday_calendar.describe(to_ist_ts("2026-11-16 10:00")) == "holiday"

    def test_session_bounds_and_elapsed(self, calendar: TradingCalendar) -> None:
        start, end = calendar.session_bounds(to_ist_ts("2026-11-16 11:00"))
        assert start.strftime("%H:%M") == "09:15"
        assert end.strftime("%H:%M") == "15:30"
        assert calendar.session_minutes_elapsed(to_ist_ts("2026-11-16 10:15")) == 60

    def test_next_open_skips_weekend(self, calendar: TradingCalendar) -> None:
        friday_close = to_ist_ts("2026-11-13 16:00")
        assert calendar.next_open(friday_close).date() == dt.date(2026, 11, 16)


class TestHolidayLoading:
    def test_list_format(self, tmp_path) -> None:
        path = tmp_path / "h.yaml"
        path.write_text("holidays: [2026-01-26, 2026-03-04]\n", encoding="utf-8")
        assert len(load_holidays(path)) == 2

    def test_mapping_format(self, tmp_path) -> None:
        path = tmp_path / "h.yaml"
        path.write_text("holidays:\n  2026-01-26: Republic Day\n", encoding="utf-8")
        holidays = load_holidays(path)
        assert dt.date(2026, 1, 26) in holidays

    def test_empty_file_is_safe(self, tmp_path) -> None:
        path = tmp_path / "h.yaml"
        path.write_text("holidays: []\n", encoding="utf-8")
        assert load_holidays(path) == frozenset()

    def test_missing_file_is_safe(self, tmp_path) -> None:
        assert load_holidays(tmp_path / "absent.yaml") == frozenset()
        assert load_holidays(None) == frozenset()

    def test_invalid_date_raises(self, tmp_path) -> None:
        path = tmp_path / "h.yaml"
        path.write_text("holidays: ['not-a-date']\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not a date"):
            load_holidays(path)

    def test_invalid_yaml_raises(self, tmp_path) -> None:
        path = tmp_path / "h.yaml"
        path.write_text("holidays: [unclosed\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="YAML"):
            load_holidays(path)
