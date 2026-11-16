"""F1: the dashboard must actually start the Kite ticker.

Before P0 nothing called ``LiveFeedService.start()``: ``/api/data-sources``
advertised live data, the chart offered the source, and every live request
returned an empty series while ``status().state`` stayed ``disconnected`` with
zero subscriptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kite_alerts.ui.app import UiContext
from kite_alerts.ui.feed_controller import FeedController
from unit.ui_support import make_client, make_context, make_settings


@dataclass
class FakeFeed:
    is_simulated: bool = False
    subscribed: list[tuple[str, int, bool]] = field(default_factory=list)
    started: list[tuple[str, str]] = field(default_factory=list)
    bootstrap: list[tuple[str, int]] = field(default_factory=list)
    state: str = "disconnected"
    stopped: bool = False

    def subscribe(self, symbols: list[tuple[str, int, bool]]) -> None:
        self.subscribed.extend(symbols)

    def start(self, *, api_key: str, access_token: str) -> None:
        self.started.append((api_key, access_token))
        self.state = "connecting"

    def run_bootstrap(self, symbol: str, token: int) -> None:
        self.bootstrap.append((symbol, token))

    def status(self) -> Any:
        return SimpleNamespace(state=self.state, subscribed=len(self.subscribed))

    def stop(self) -> None:
        self.stopped = True


class FakeResolver:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def resolve(self, spec: str) -> Any:
        self.asked.append(spec)
        return SimpleNamespace(symbol=spec.upper(), token=1001, is_future=False)


def _controller(settings: Any, feed: FakeFeed, resolver: FakeResolver) -> FeedController:
    return FeedController(
        feed,
        lambda: settings,
        resolver_factory=lambda key, token, cache_dir: resolver,
    )


class TestController:
    def test_resolves_subscribes_starts_and_bootstraps_once(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path / "alerts.db")
        feed = FakeFeed()
        resolver = FakeResolver()
        controller = _controller(settings, feed, resolver)

        assert controller.ensure_started() is True
        assert resolver.asked == ["NSE:TEST"]
        assert feed.subscribed == [("NSE:TEST", 1001, False)]
        assert feed.started == [("test_key", settings.kite.token)]
        assert feed.bootstrap == [("NSE:TEST", 1001)]

        # Asking again (lifespan + reload + chart) must not double-bootstrap.
        assert controller.ensure_started() is True
        assert feed.bootstrap == [("NSE:TEST", 1001)]
        assert len(feed.started) == 2  # start() itself is idempotent in the feed

    def test_without_a_token_nothing_starts(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path / "alerts.db")
        from pydantic import SecretStr

        settings = settings.model_copy(
            update={"kite": settings.kite.model_copy(update={"access_token": SecretStr("")})}
        )
        feed = FakeFeed()

        controller = _controller(settings, feed, FakeResolver())

        assert controller.ensure_started() is False
        assert feed.started == []
        assert feed.subscribed == []

    def test_simulated_mode_leaves_the_feed_to_the_simulator(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path / "alerts.db")
        feed = FakeFeed(is_simulated=True)

        controller = _controller(settings, feed, FakeResolver())

        assert controller.ensure_started() is True
        assert feed.started == []

    def test_resolution_failures_do_not_raise(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path / "alerts.db")
        feed = FakeFeed()

        class Broken:
            def resolve(self, spec: str) -> Any:
                raise ValueError("no dump")

        controller = FeedController(
            feed, lambda: settings, resolver_factory=lambda k, t, c: Broken()
        )

        assert controller.ensure_started() is False
        assert feed.started == []


class TestAppWiring:
    def test_lifespan_starts_the_feed(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path / "alerts.db")
        context = make_context(settings)
        feed = FakeFeed()
        context.feed_controller = _controller(settings, feed, FakeResolver())

        with make_client(context):
            pass

        assert feed.started == [("test_key", settings.kite.token)]
        assert feed.subscribed == [("NSE:TEST", 1001, False)]

    def test_chart_request_starts_the_feed_for_the_selected_symbol(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path / "alerts.db")
        context = make_context(settings)
        feed = FakeFeed()
        resolver = FakeResolver()
        context.feed_controller = _controller(settings, feed, resolver)
        context.feed = feed
        if context.chart is not None and "kite_live" in context.chart.sources:
            context.chart.sources["kite_live"].feed_provider = lambda: feed

        with make_client(context) as client:
            resolver.asked.clear()
            client.get(
                "/api/chart/candles",
                params={"source": "kite_live", "symbol": "NSE:INFY"},
            )

        assert "NSE:INFY" in resolver.asked
        assert ("NSE:INFY", 1001, False) in feed.subscribed

    def test_non_live_sources_never_start_the_feed(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path / "alerts.db")
        context = make_context(settings)
        feed = FakeFeed()
        context.feed_controller = _controller(settings, feed, FakeResolver())

        with make_client(context) as client:
            feed.started.clear()  # the lifespan already started it on boot
            client.get("/api/chart/candles", params={"source": "demo", "symbol": "NSE:TEST"})

        assert feed.started == []


def test_ui_context_builds_a_controller(tmp_path: Path) -> None:
    """Every real context must own a controller, not ``None``."""
    context: UiContext = make_context(make_settings(tmp_path / "wiring.db"))
    assert isinstance(context.feed_controller, FeedController)
