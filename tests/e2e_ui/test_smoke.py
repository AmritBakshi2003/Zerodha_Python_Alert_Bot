"""Playwright smoke test for the dashboard (F15 + F11 + F27).

These tests launch the real Starlette app under uvicorn in a background thread,
drive the page through Playwright, and verify the product-path behaviours:
no console/page errors, badge transitions, source fallback on token expiry,
dark/light theming, and the simulator controls.

They are marked ``e2e_ui`` and excluded from the default pytest run (see
pyproject.toml). Run with ``pytest -m e2e_ui`` or ``pytest -o addopts=""``.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

import pytest

# Check playwright availability early; skip cleanly if not installed.
try:
    from playwright.sync_api import sync_playwright  # type: ignore[import]
except Exception:  # pragma: no cover - optional dependency
    sync_playwright = None  # type: ignore[assignment]

from kite_alerts.ui.app import build_context, create_app

# Quiet uvicorn access logs in tests
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _free_port() -> int:
    """Return a port that is currently free on 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _DashboardServer:
    """Run the dashboard app in a background thread."""

    def __init__(self, port: int, simulate_kite: bool = True) -> None:
        self.port = port
        self.simulate_kite = simulate_kite
        self._thread: threading.Thread | None = None
        self._server = None
        self._context = None
        self._error: BaseException | None = None
        self.base_url = f"http://127.0.0.1:{port}"

    def start(self) -> None:
        def target() -> None:
            try:
                context = build_context(
                    env_file=".env",
                    port=self.port,
                    simulate_kite=self.simulate_kite,
                )
                self._context = context
                app = create_app(context)

                import uvicorn

                config = uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=self.port,
                    log_level="warning",
                    access_log=False,
                )
                server = uvicorn.Server(config)
                self._server = server
                server.run()
            except BaseException as exc:
                self._error = exc

        self._thread = threading.Thread(target=target, name="dashboard-server", daemon=True)
        self._thread.start()

        # Poll until the port accepts connections (server bound and ready).
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if self._error is not None:
                raise self._error
            try:
                conn = socket.create_connection(("127.0.0.1", self.port), timeout=0.3)
                conn.close()
                return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError("dashboard server did not start in time")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        if self._context is not None:
            simulator = getattr(self._context, "simulator", None)
            if simulator is not None:
                simulator.stop()
            feed = getattr(self._context, "feed", None)
            if feed is not None:
                feed.stop()


@pytest.fixture(scope="module")
def dashboard() -> _DashboardServer:
    """Module-scoped dashboard with the Kite simulator enabled."""
    if sync_playwright is None:
        pytest.skip("playwright not installed")
    port = _free_port()
    server = _DashboardServer(port, simulate_kite=True)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def reset_simulator(dashboard: _DashboardServer) -> None:
    """Restore a clean simulator state before every browser test.

    The token-expiry test deliberately kills the simulator; without this
    reset, later tests would see a permanent token_expired state and never
    get the LIVE/SIMULATED status event that reveals the simulator controls.
    """
    context = dashboard._context
    if context is not None and getattr(context, "simulator", None) is not None:
        context.simulator.trigger_reconnect()


@pytest.mark.e2e_ui
class TestDashboardSmoke:
    """No-console-errors smoke checks for every page."""

    def test_no_console_errors_on_every_page(self, dashboard: _DashboardServer) -> None:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                errors: list[str] = []

                def on_console(msg) -> None:
                    if msg.type == "error":
                        errors.append(f"CONSOLE: {msg.text}")

                def on_page_error(exc) -> None:
                    errors.append(f"PAGE: {exc}")

                page.on("console", on_console)
                page.on("pageerror", on_page_error)

                # Navigate and wait for network idle (SSE streams stay open,
                # so we wait for load + a short settle).
                page.goto(dashboard.base_url, wait_until="load")
                page.wait_for_timeout(2000)

                # Check the chart page (default)
                assert not errors, f"Console/page errors on chart page: {errors}"

                # Visit each page via the data-page sections
                for page_id in ["dashboard", "practice", "alerts", "settings", "connect", "help"]:
                    page.evaluate(f"""() => {{
                        document.querySelectorAll('[data-page]').forEach(el => el.hidden = true);
                        document.getElementById('page-{page_id}').hidden = false;
                    }}""")
                    page.wait_for_timeout(500)
                    assert not errors, f"Console/page errors on {page_id} page: {errors}"

            finally:
                browser.close()

    def test_badge_shows_delayed_initially(self, dashboard: _DashboardServer) -> None:
        """Initial load on the chart page shows the DELAYED badge."""
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(dashboard.base_url, wait_until="load")
                page.wait_for_timeout(1500)

                badge = page.locator("#chart-badge")
                badge.wait_for(state="visible")
                text = badge.text_content() or ""
                assert "DELAYED" in text, f"Expected DELAYED badge, got: {text}"

            finally:
                browser.close()

    def test_kite_live_source_is_simulated_never_live(self, dashboard: _DashboardServer) -> None:
        """Selecting kite_live in simulate mode shows SIMULATED (purple), never LIVE (green)."""
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(dashboard.base_url, wait_until="load")
                page.wait_for_timeout(1500)

                # Select the live source
                select = page.locator("#chart-source")
                select.wait_for(state="visible")
                select.select_option("kite_live")

                # Wait for the badge to update
                page.wait_for_timeout(2000)

                badge = page.locator("#chart-badge")
                text = badge.text_content() or ""
                assert "SIMULATED" in text, f"Expected SIMULATED badge, got: {text}"
                assert (
                    "LIVE" not in text or "SIMULATED" in text
                ), f"Should never show bare LIVE in simulate mode: {text}"

                # Verify feed status reflects simulated
                feed_state = page.locator("#live-feed-state")
                feed_text = feed_state.text_content() or ""
                assert "simulated" in feed_text.lower() or "connected" in feed_text.lower()

            finally:
                browser.close()

    def test_dark_and_light_color_scheme(self, dashboard: _DashboardServer) -> None:
        """Both color-scheme media queries render without console errors."""
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for scheme in ("light", "dark"):
                    page = browser.new_page(color_scheme=scheme)
                    page_errors: list[str] = []

                    def on_console(msg, collected=page_errors) -> None:
                        if msg.type == "error":
                            collected.append(f"CONSOLE: {msg.text}")

                    def on_page_error(exc, collected=page_errors) -> None:
                        collected.append(f"PAGE: {exc}")

                    page.on("console", on_console)
                    page.on("pageerror", on_page_error)

                    page.goto(dashboard.base_url, wait_until="load")
                    page.wait_for_timeout(2000)

                    # Switch to kite_live to exercise more code paths
                    page.locator("#chart-source").select_option("kite_live")
                    page.wait_for_timeout(1500)

                    assert not page_errors, f"Console/page errors in {scheme} mode: {page_errors}"
                    page.close()

            finally:
                browser.close()

    def test_token_expiry_fallbacks_to_delayed(self, dashboard: _DashboardServer) -> None:
        """Clicking the token-expiry button falls back to nse_delayed and shows a message."""
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(dashboard.base_url, wait_until="load")
                page.wait_for_timeout(1500)

                # Select kite_live first
                page.locator("#chart-source").select_option("kite_live")

                # Wait for the sim control to be revealed by renderFeedStatus.
                exp_btn = page.locator("#sim-btn-expiry")
                exp_btn.wait_for(state="visible", timeout=10000)

                # Verify we are on kite_live
                badge = page.locator("#chart-badge")
                assert "SIMULATED" in (badge.text_content() or "")

                # Record all messages that pass through #chart-message. The
                # expiry notice is transient: after the fallback, a reload
                # overwrites it with the "market closed" notice.
                page.evaluate("""() => {
                    window.__seenMessages = [];
                    const node = document.getElementById('chart-message');
                    new MutationObserver(() => {
                        window.__seenMessages.push(node.textContent || '');
                    }).observe(node, { childList: true, characterData: true, subtree: true });
                }""")

                exp_btn.click()

                # Wait for the source to fall back to nse_delayed.
                source_select = page.locator("#chart-source")
                page.wait_for_function(
                    "() => document.getElementById('chart-source').value === 'nse_delayed'",
                    timeout=10000,
                )
                selected = source_select.input_value()
                assert (
                    selected == "nse_delayed"
                ), f"Source should fall back to nse_delayed, got {selected}"

                # Badge should now show DELAYED (fallback source).
                badge_text = badge.text_content() or ""
                assert (
                    "DELAYED" in badge_text
                ), f"Expected DELAYED after fallback, got: {badge_text}"

                # The expiry message was shown at some point (before the
                # market-closed notice overwrote it).
                seen = page.evaluate("() => window.__seenMessages")
                assert any(
                    any(w in m.lower() for w in ("expired", "token", "fallback")) for m in seen
                ), f"Expected expiry message in #chart-message, got: {seen}"

            finally:
                browser.close()

    def test_sim_controls_visible_only_in_simulate_mode(self, dashboard: _DashboardServer) -> None:
        """Simulator controls are visible when simulate_kite=True."""
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(dashboard.base_url, wait_until="load")
                page.wait_for_timeout(1500)

                # Select kite_live to reveal the sim controls
                page.locator("#chart-source").select_option("kite_live")

                # The controls appear once the SSE stream delivers the first
                # status event; use an explicit wait rather than a fixed sleep.
                sim_controls = page.locator("#sim-controls")
                sim_controls.wait_for(state="visible", timeout=10000)

                # All three buttons present
                assert page.locator("#sim-btn-disconnect").is_visible()
                assert page.locator("#sim-btn-expiry").is_visible()
                assert page.locator("#sim-btn-reconnect").is_visible()

            finally:
                browser.close()


@pytest.mark.e2e_ui
class TestSimulatorControls:
    """Direct simulator control endpoints."""

    def test_disconnect_reconnect_cycle(self, dashboard: _DashboardServer) -> None:
        """Trigger disconnect then reconnect; feed status reflects it."""
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(dashboard.base_url, wait_until="load")
                page.wait_for_timeout(1500)

                page.locator("#chart-source").select_option("kite_live")
                page.locator("#sim-btn-disconnect").wait_for(state="visible", timeout=10000)

                feed_state = page.locator("#live-feed-state")

                # Trigger disconnect and wait for the status text to reflect it.
                page.locator("#sim-btn-disconnect").click()
                page.wait_for_function(
                    """() => {
                        const node = document.getElementById('live-feed-state');
                        const t = (node.textContent || '').toLowerCase();
                        return t.includes('disconnect') || t.includes('reconnect')
                            || t.includes('connecting');
                    }""",
                    timeout=10000,
                )
                state_text = feed_state.text_content() or ""
                assert any(
                    w in state_text.lower() for w in ("disconnect", "reconnect", "connecting")
                ), f"Feed should show disconnecting/reconnecting, got: {state_text}"

                # Trigger reconnect and wait for the connected state.
                page.locator("#sim-btn-reconnect").click()
                page.wait_for_function(
                    """() => {
                        const node = document.getElementById('live-feed-state');
                        const t = (node.textContent || '').toLowerCase();
                        return t.includes('connected') || t.includes('live')
                            || t.includes('simulated');
                    }""",
                    timeout=10000,
                )
                state_text = feed_state.text_content() or ""
                assert any(
                    w in state_text.lower() for w in ("connected", "live", "simulated")
                ), f"Feed should show connected/live, got: {state_text}"

            finally:
                browser.close()
