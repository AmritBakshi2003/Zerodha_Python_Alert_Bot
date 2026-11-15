"""Static guarantees for the Chart page: badge, default route, disabled Kite."""

from __future__ import annotations

import re
from pathlib import Path

import kite_alerts

STATIC = Path(kite_alerts.__file__).parent / "ui" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


class TestChartPage:
    def test_chart_is_the_first_sidebar_item_and_default_route(self) -> None:
        routes = re.findall(r'data-route="([a-z]+)"', read("index.html"))
        assert routes[0] == "chart"
        app = read("app.js")
        assert 'const ROUTES = ["chart", "dashboard"' in app
        assert 'return ROUTES.indexOf(raw) >= 0 ? raw : "chart";' in app

    def test_every_source_has_a_permanent_high_contrast_badge(self) -> None:
        html = read("index.html")
        assert 'id="chart-badge"' in html
        for badge in ("DELAYED", "SHOWCASE", "DEMO", "LIVE"):
            assert f'data-badge="{badge}"' in html
        css = read("styles.css")
        for badge in ("DELAYED", "SHOWCASE", "DEMO", "LIVE"):
            assert f'.source-badge[data-badge="{badge}"]' in css

    def test_badge_text_is_in_the_png_export(self) -> None:
        chart = read("chart.js")
        assert "takeScreenshot" in chart
        assert "toDataURL" in chart
        assert "BADGE_MESSAGES" in chart
        for text in (
            "DELAYED — unofficial, delayed data",
            "SHOWCASE — crypto, not NSE data",
            "DEMO",
        ):
            assert text in chart

    def test_source_picker_disables_kite_live(self) -> None:
        chart = read("chart.js")
        assert "kite_live" in chart
        assert "enabled: false" in chart
        assert "option.disabled = source.enabled === false" in chart
        assert "later update" in chart
        assert "SOURCE_ORDER" in chart
        assert "nse_delayed: 0, crypto_showcase: 1, demo: 2, kite_live: 3" in chart

    def test_chart_types_and_timeframes_are_all_offered(self) -> None:
        html = read("index.html")
        for option in ("candles", "hollow", "heikin", "bars", "line", "area", "baseline"):
            assert f'value="{option}"' in html
        for interval in ("1", "3", "5", "15"):
            assert f'value="{interval}"' in html

    def test_freshness_and_signal_copy(self) -> None:
        chart = read("chart.js")
        assert '"Latest candle " + fresh.latest_time + " IST, " + fresh.age_text' in chart
        assert '"Last checked " + fresh.checked_at' in chart
        assert "stale_since" in chart
        service = (STATIC.parent / "chart" / "service.py").read_text(encoding="utf-8")
        assert "Informational, based on" in service

    def test_no_provider_urls_in_the_browser(self) -> None:
        for name in ("chart.js", "app.js"):
            text = read(name).replace("http://www.w3.org/2000/svg", "")
            assert "api.kraken.com" not in text
            assert "http://" not in text
            assert "https://" not in text

    def test_chart_options_are_keyboard_accessible(self) -> None:
        html = read("index.html")
        assert 'aria-label="Chart status"' in html
        assert 'aria-label="Data honesty badges"' in html
        assert 'role="status"' in html
        assert "summary" in html
