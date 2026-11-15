"""Sidebar layout, hash routing and the static wording of the C2 pages."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import kite_alerts

UI = Path(kite_alerts.__file__).parent / "ui"
STATIC = UI / "static"
ROUTES = ["chart", "dashboard", "practice", "alerts", "settings", "connect", "help"]


def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def module(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


class TestSidebar:
    def test_chart_is_first_and_all_routes_are_in_the_sidebar_in_order(self) -> None:
        routes = re.findall(r'data-route="([a-z]+)"', html())
        assert routes == ROUTES

    def test_every_route_has_a_page_section(self) -> None:
        pages = re.findall(r'data-page="([a-z]+)"', html())
        assert pages == ROUTES

    def test_only_the_chart_is_visible_initially(self) -> None:
        for match in re.finditer(
            r'<section class="page"[^>]*data-page="([a-z]+)"([^>]*)>', html(), re.S
        ):
            route, attributes = match.groups()
            assert ("hidden" in attributes) is (route != "chart")

    def test_the_nav_is_accessible(self) -> None:
        text = html()
        assert 'aria-label="Pages"' in text
        assert 'class="skip-link"' in text
        assert 'href="#main"' in text
        assert 'id="main" tabindex="-1"' in text
        assert "<nav" in text

    def test_the_router_is_hash_based_and_titles_the_page(self) -> None:
        source = module("app.js")
        assert 'window.addEventListener("hashchange", routeChange)' in source
        assert (
            'const ROUTES = ["chart", "dashboard", "practice", "alerts", "settings", '
            '"connect", "help"]' in source
        )
        assert "aria-current" in source
        assert 'document.title = "Kite Alerts — "' in source
        assert "section.hidden = section.dataset.page !== route" in source


class TestPageContent:
    def test_existing_c1_pages_keep_their_content(self) -> None:
        text = html()
        for element in (
            'id="bot-status-label"',
            'id="alert-feed"',
            'id="history-body"',
            'id="test-alert-button"',
            'id="demo-play"',
            'id="about-alerts-only"',
            "never places, modifies or cancels orders",
        ):
            assert element in text

    def test_practice_is_labelled_demo_and_not_written_to_history(self) -> None:
        text = html()
        practice = text.split('data-page="practice"', 1)[1].split('data-page="alerts"', 1)[0]
        assert "DEMO" in practice
        assert "nothing is saved to your alert history" in practice.lower()
        source = module("practice.js")
        assert '"/api/demo/timeline"' in source
        assert '"/api/practice/check"' in source
        assert "/api/alerts" not in source
        assert "/api/test-alert" not in source

    def test_first_run_welcome_is_local_and_on_the_practice_page(self) -> None:
        source = module("practice.js")
        assert 'const WELCOME_KEY = "kiteAlertsWelcomeDismissed"' in source
        assert "localStorage.getItem" in source
        assert "localStorage.setItem" in source
        text = html()
        assert 'id="welcome"' in text
        assert 'id="welcome-dismiss"' in text
        practice = text.split('data-page="practice"', 1)[1].split('data-page="alerts"', 1)[0]
        assert 'id="welcome"' in practice

    def test_test_my_settings_button_uses_saved_parameters(self) -> None:
        text = html()
        assert "Test my settings" in text
        assert 'id="practice-check"' in text
        source = module("practice.js")
        assert "settings saved on disk" in source
        assert "data.counts.buys" in source
        assert "data.counts.sells" in source

    def test_connect_wizard_has_the_four_steps(self) -> None:
        text = html()
        connect = text.split('data-page="connect"', 1)[1].split('data-page="help"', 1)[0]
        assert "developers.kite.trade" in connect
        assert "paid Kite Connect plan" in connect
        assert "never guesses a price" in connect
        assert 'id="connect-redirect"' in connect
        assert 'id="connect-key"' in connect
        assert 'type="password"' in connect
        assert 'id="connect-start"' in connect
        assert 'id="connect-manual-token"' in connect
        assert "request_token" in connect
        assert 'id="connect-disconnect"' in connect
        assert 'id="data-sources"' in connect

    def test_the_manual_fallback_is_easy_to_find(self) -> None:
        connect = html().split('data-page="connect"', 1)[1]
        assert "<details" in connect
        assert "Zerodha didn't come back? Paste the request_token instead" in connect

    def test_settings_page_notices_and_guides(self) -> None:
        text = html()
        settings = text.split('data-page="settings"', 1)[1].split('data-page="connect"', 1)[0]
        assert 'id="settings-applies"' in settings
        assert 'id="settings-check"' in settings
        assert "@BotFather" in settings
        assert 'id="telegram-find"' in settings
        assert 'id="test-alert-button"' in settings
        assert "Slack" in settings
        source = module("settings.js")
        assert "data.applies_notice" in source
        assert '"/api/notifications/telegram/chat-id"' in source

    def test_help_has_the_troubleshooting_list(self) -> None:
        help_page = html().split('data-page="help"', 1)[1]
        for phrase in (
            "Redirect URL mismatch",
            "Token expired",
            "no market data",
            "Port in use",
        ):
            assert phrase.lower() in help_page.lower()


class TestStaticHygiene:
    def test_modules_are_served_and_imported(self) -> None:
        app = module("app.js")
        for name in ("connect", "practice", "settings", "chart"):
            assert f'from "./{name}.js"' in app
        assert 'from "./dom.js"' in app

    def test_no_markup_injection(self) -> None:
        for path in sorted(STATIC.glob("*.js")):
            text = path.read_text(encoding="utf-8")
            for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
                assert forbidden not in text, f"{path.name} uses {forbidden}"
        assert "textContent" in module("dom.js")

    def test_no_cdn_or_external_script(self) -> None:
        text = html()
        assert 'src="http' not in text
        assert '<link rel="stylesheet" href="http' not in text
        for forbidden in ("cdn.", "unpkg", "jsdelivr", "fonts.googleapis", "googleapis"):
            for path in sorted(STATIC.glob("*")):
                if path.suffix in {".js", ".css", ".html"}:
                    assert forbidden not in path.read_text(encoding="utf-8"), path.name

    def test_styles_are_complete_and_theme_aware(self) -> None:
        css = module("styles.css")
        assert ".sidebar" in css
        assert "@media (prefers-color-scheme: dark)" in css
        assert ":focus-visible" in css
        assert "@media (max-width" in css

    @pytest.mark.parametrize(
        "name",
        ["app.js", "dom.js", "practice.js", "settings.js", "connect.js", "styles.css"],
    )
    def test_assets_are_served(self, tmp_path: Path, name: str) -> None:
        from unit.ui_support_c2 import make_c2_client, make_c2_context

        context, _env = make_c2_context(tmp_path)
        with make_c2_client(context) as client:
            response = client.get(f"/static/{name}")
            assert response.status_code == 200
            assert "text/html" not in response.headers["content-type"]
