"""F4: the market clock must name the real NSE phase for every minute.

On the pre-P1 code ``describe()`` returned ``pre-open`` for 01:40 and
``post-close`` for 19:00, so the dashboard was wrong for ~18 hours a day, and
there was no notion of the 09:00-09:15 pre-open, the 15:30-16:00 closing
window, or a configured special session.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from kite_alerts.core import calendar as cal
from kite_alerts.core.clock import IST

DOCUMENTED_STATES = {
    "closed",
    "pre-open",
    "open",
    "closing",
    "weekend",
    "holiday",
    "special-session",
}
MONDAY = dt.date(2026, 11, 16)
SATURDAY = dt.date(2026, 11, 14)


def _at(day: dt.date, hour: int, minute: int = 0) -> pd.Timestamp:
    return pd.Timestamp(dt.datetime.combine(day, dt.time(hour, minute), IST))


@pytest.fixture
def calendar() -> cal.TradingCalendar:
    return cal.TradingCalendar()


class TestExhaustiveMapping:
    def test_every_minute_of_a_trading_day_has_exactly_one_state(
        self, calendar: cal.TradingCalendar
    ) -> None:
        states: list[str] = []
        for minute in range(24 * 60):
            moment = _at(MONDAY, 0) + pd.Timedelta(minutes=minute)
            info = calendar.session_info(moment)

            assert info.state in DOCUMENTED_STATES
            assert info.label, f"no label at {moment}"
            assert info.detail, f"no detail at {moment}"
            # The guard and the label can never disagree.
            assert info.tradable == calendar.in_session(moment)
            assert calendar.describe(moment) == info.state
            states.append(info.state)

        # One forward pass through the day: no state may come back.
        order: list[str] = []
        for state in states:
            if not order or order[-1] != state:
                order.append(state)
        assert order == ["closed", "pre-open", "open", "closing", "closed"]

    def test_tradable_window_is_still_0915_to_1530(self, calendar: cal.TradingCalendar) -> None:
        for minute in range(24 * 60):
            moment = _at(MONDAY, 0) + pd.Timedelta(minutes=minute)
            expected = dt.time(9, 15) <= moment.time() <= dt.time(15, 30)
            assert calendar.session_info(moment).tradable is expected

    @pytest.mark.parametrize(
        ("hour", "minute", "state"),
        [
            (1, 40, "closed"),  # the bug: this used to read "pre-open"
            (8, 59, "closed"),
            (9, 0, "pre-open"),
            (9, 9, "pre-open"),
            (9, 10, "pre-open"),
            (9, 12, "pre-open"),
            (9, 14, "pre-open"),
            (9, 15, "open"),
            (15, 29, "open"),
            (15, 30, "open"),
            (15, 31, "closing"),  # used to read "post-close"
            (16, 0, "closing"),
            (16, 1, "closed"),
            (23, 59, "closed"),
        ],
    )
    def test_boundary_minutes(
        self, calendar: cal.TradingCalendar, hour: int, minute: int, state: str
    ) -> None:
        assert calendar.session_info(_at(MONDAY, hour, minute)).state == state

    @pytest.mark.parametrize(
        ("hour", "minute", "phase"),
        [(9, 0, "entry"), (9, 9, "entry"), (9, 10, "matching"), (9, 12, "buffer")],
    )
    def test_pre_open_sub_phases(
        self, calendar: cal.TradingCalendar, hour: int, minute: int, phase: str
    ) -> None:
        info = calendar.session_info(_at(MONDAY, hour, minute))
        assert info.pre_open_phase == phase

    def test_closed_detail_names_the_next_open(self, calendar: cal.TradingCalendar) -> None:
        info = calendar.session_info(_at(MONDAY, 1, 40))
        assert "09:15" in info.detail
        assert pd.Timestamp(info.opens_at).strftime("%H:%M") == "09:15"
        assert pd.Timestamp(info.opens_at).date() == MONDAY


class TestNonTradingDays:
    def test_weekend(self, calendar: cal.TradingCalendar) -> None:
        info = calendar.session_info(_at(SATURDAY, 10, 0))
        assert info.state == "weekend"
        assert info.tradable is False

    def test_holiday_beats_the_weekday_rule(self) -> None:
        holiday_calendar = cal.TradingCalendar(holidays=[MONDAY])
        info = holiday_calendar.session_info(_at(MONDAY, 10, 0))
        assert info.state == "holiday"
        assert info.tradable is False
        assert "holiday" in info.label.lower()


class TestSpecialSessions:
    def _calendar(self) -> cal.TradingCalendar:
        return cal.TradingCalendar(
            special_sessions=[
                cal.SpecialSession(
                    day=SATURDAY,
                    open_time=dt.time(18, 15),
                    close_time=dt.time(19, 15),
                    label="Muhurat trading",
                )
            ]
        )

    def test_special_session_overrides_the_weekend_rule(self) -> None:
        calendar = self._calendar()
        info = calendar.session_info(_at(SATURDAY, 18, 30))

        assert info.state == "special-session"
        assert info.tradable is True
        assert info.is_special is True
        assert "Muhurat" in info.label
        assert calendar.in_session(_at(SATURDAY, 18, 30))

    def test_special_session_day_is_still_closed_around_the_window(self) -> None:
        calendar = self._calendar()
        assert calendar.session_info(_at(SATURDAY, 12, 0)).state == "weekend"
        assert calendar.session_info(_at(SATURDAY, 19, 30)).state == "closed"
        assert not calendar.in_session(_at(SATURDAY, 12, 0))

    def test_special_session_wins_over_a_holiday(self) -> None:
        calendar = cal.TradingCalendar(
            holidays=[SATURDAY],
            special_sessions=[
                cal.SpecialSession(SATURDAY, dt.time(18, 15), dt.time(19, 15), "Muhurat")
            ],
        )
        assert calendar.session_info(_at(SATURDAY, 18, 30)).tradable is True


class TestSerialisation:
    def test_session_info_serialises_for_the_ui(self) -> None:
        info = cal.TradingCalendar().session_info(_at(MONDAY, 10, 0))
        payload = info.as_dict()

        assert payload["state"] == "open"
        assert payload["tradable"] is True
        assert payload["session_open"] == "09:15"
        assert payload["session_close"] == "15:30"
        assert payload["now_ist"].startswith("10:00")
