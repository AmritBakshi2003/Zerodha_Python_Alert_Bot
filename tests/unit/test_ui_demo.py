"""The demo player: same engine, same alerts as ``demo_output.txt``."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from kite_alerts.config.settings import StrategyParams
from kite_alerts.demo import synthetic_day
from kite_alerts.ui.demo_player import DemoTimeline, build_demo_timeline
from kite_alerts.ui.series import build_chart, chart_time, number
from unit.ui_support import REPO_ROOT

ALLOWED_STATUSES = {"agree", "against", "neutral", "unavailable"}


def golden_messages() -> list[str]:
    lines = (REPO_ROOT / "demo_output.txt").read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.startswith("3 alert(s)")]


@pytest.fixture(scope="module")
def timeline() -> DemoTimeline:
    return build_demo_timeline()


class TestDemoTimeline:
    def test_exactly_the_three_expected_alerts(self, timeline: DemoTimeline) -> None:
        assert [alert.side for alert in timeline.alerts] == ["SELL", "SELL", "BUY"]
        assert timeline.sells == 2
        assert timeline.buys == 1
        assert len(timeline.alerts) == 3

    def test_alert_text_matches_the_golden_demo_output(self, timeline: DemoTimeline) -> None:
        assert [alert.message for alert in timeline.alerts] == golden_messages()

    def test_alert_times_are_0934_0949_1007(self, timeline: DemoTimeline) -> None:
        times = [pd.Timestamp(alert.ts).strftime("%H:%M") for alert in timeline.alerts]
        assert times == ["09:34", "09:49", "10:07"]

    def test_alert_markers_point_at_real_candles(self, timeline: DemoTimeline) -> None:
        assert len(timeline.candles) == 151
        indices = [alert.index for alert in timeline.alerts]
        assert indices == sorted(indices)
        for alert in timeline.alerts:
            assert 0 <= alert.index < len(timeline.candles)
            assert timeline.candles[alert.index]["time"] == alert.chart_time

    def test_rule_evidence_matches_the_alert(self, timeline: DemoTimeline) -> None:
        labels = {"ema_cross": "EMA", "rsi_zone": "RSI", "macd": "MACD", "orb": "ORB", "oi": "OI"}
        for alert in timeline.alerts:
            assert {rule["id"] for rule in alert.rules} == set(labels)
            for rule in alert.rules:
                assert rule["label"] == labels[rule["id"]]
                assert rule["status"] in ALLOWED_STATUSES
            agreeing = {rule["id"] for rule in alert.rules if rule["status"] == "agree"}
            assert agreeing == set(alert.rules_used)

        first_sell = timeline.alerts[0]
        statuses = {rule["id"]: rule["status"] for rule in first_sell.rules}
        assert statuses["rsi_zone"] == "agree"
        assert statuses["macd"] == "agree"
        assert statuses["orb"] == "agree"
        assert statuses["oi"] == "agree"
        assert statuses["ema_cross"] != "agree"

        buy = timeline.alerts[2]
        buy_statuses = {rule["id"]: rule["status"] for rule in buy.rules}
        assert buy_statuses["ema_cross"] == "agree"
        assert buy_statuses["rsi_zone"] == "agree"
        assert buy_statuses["macd"] == "agree"

    def test_opening_range_band(self, timeline: DemoTimeline) -> None:
        assert timeline.orb is not None
        assert timeline.orb["high"] > timeline.orb["low"]
        assert timeline.orb["formed_at"] > timeline.orb["start"]
        assert "Opening range" in timeline.orb["label"]

    def test_payload_is_labelled_demo(self, timeline: DemoTimeline) -> None:
        payload = timeline.payload()
        assert payload["demo"] is True
        assert payload["label"] == "DEMO"
        assert payload["symbol"] == "DEMO:FUT"
        assert payload["counts"] == {"buys": 1, "sells": 2, "total": 3}
        assert payload["speeds"] == [1, 5, 20, 60]
        assert all(alert["demo"] and alert["label"] == "DEMO" for alert in payload["alerts"])
        assert "not saved" in payload["note"] or "Nothing here is saved" in payload["note"]

    def test_event_stream_replays_candles_then_alerts(self, timeline: DemoTimeline) -> None:
        events = list(timeline.events())
        candles = [event for event in events if event["type"] == "candle"]
        alerts = [event for event in events if event["type"] == "alert"]
        assert len(candles) == 151
        assert [event["alert"]["side"] for event in alerts] == ["SELL", "SELL", "BUY"]
        assert events[-1]["type"] == "end"
        assert events[-1]["counts"] == {"buys": 1, "sells": 2, "total": 3}
        for event in alerts:
            assert event["index"] == event["alert"]["index"]


class TestChartSeries:
    def test_candles_carry_every_indicator(self) -> None:
        frame = synthetic_day()
        chart = build_chart(frame, StrategyParams(ema_cross_lookback=5))
        assert chart.session_date == "2026-11-16"
        assert len(chart.candles) == 151
        first = chart.candles[0]
        assert first["time"] == int(
            dt.datetime(2026, 11, 16, 9, 15, tzinfo=dt.timezone.utc).timestamp()
        )
        for key in ("open", "high", "low", "close", "ema_fast", "ema_slow", "rsi", "macd"):
            assert first[key] is not None
        assert first["high"] >= first["low"]
        for candle in chart.candles:
            assert 0.0 <= candle["rsi"] <= 100.0  # type: ignore[operator]

    def test_nan_becomes_null_and_times_are_ist_wall_clock(self) -> None:
        assert number(float("nan")) is None
        assert number(float("inf")) is None
        assert number("3.5") == 3.5
        assert number(None) is None
        stamp = pd.Timestamp("2026-11-16 09:15", tz="Asia/Kolkata")
        assert chart_time(stamp) == int(
            dt.datetime(2026, 11, 16, 9, 15, tzinfo=dt.timezone.utc).timestamp()
        )

    def test_empty_frame_is_safe(self) -> None:
        chart = build_chart(pd.DataFrame(), StrategyParams())
        assert chart.candles == ()
        assert chart.orb is None
