"""Observability endpoints and the metrics registry."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from kite_alerts.observability.health import HealthServer
from kite_alerts.observability.metrics import Metrics


class TestMetrics:
    def test_counters_and_gauges(self) -> None:
        metrics = Metrics()
        metrics.inc("ticks_received")
        metrics.inc("ticks_received", 4)
        metrics.set_gauge("last_tick_age_seconds", 2.5)
        assert metrics.get("ticks_received") == 5.0
        assert metrics.get("last_tick_age_seconds") == 2.5
        assert metrics.get("unknown") == 0.0

    def test_labels_are_part_of_the_key(self) -> None:
        metrics = Metrics()
        metrics.inc("alerts_fired", 1, symbol="NSE:INFY")
        metrics.inc("alerts_fired", 2, symbol="NSE:TCS")
        assert metrics.get("alerts_fired", symbol="NSE:INFY") == 1.0
        assert metrics.get("alerts_fired", symbol="NSE:TCS") == 2.0

    def test_summary_observation(self) -> None:
        metrics = Metrics()
        for value in (1.0, 3.0, 2.0):
            metrics.observe("latency", value)
        snapshot = metrics.snapshot()
        assert snapshot["latency_count"] == 3.0
        assert snapshot["latency_sum"] == 6.0
        assert snapshot["latency_max"] == 3.0

    def test_prometheus_rendering(self) -> None:
        metrics = Metrics()
        metrics.inc("ticks_received")
        metrics.set_gauge("last_tick_age_seconds", 1.5)
        text = metrics.render_prometheus()
        assert "# TYPE kite_alerts_ticks_received counter" in text
        assert "kite_alerts_ticks_received 1.0" in text
        assert "kite_alerts_last_tick_age_seconds 1.5" in text

    def test_label_escaping(self) -> None:
        metrics = Metrics()
        metrics.inc("x", 1, label='a"b\\c')
        text = metrics.render_prometheus()
        assert '\\"b\\\\c' in text

    def test_reset(self) -> None:
        metrics = Metrics()
        metrics.inc("x")
        metrics.reset()
        assert metrics.get("x") == 0.0

    def test_series_cap_is_bounded(self) -> None:
        metrics = Metrics(max_series=2)
        metrics.inc("x", 1, a="1")
        metrics.inc("x", 1, a="2")
        metrics.inc("x", 1, a="3")
        assert len(metrics.snapshot()) == 2


@pytest.fixture
def server():
    state = {"healthy": True, "status": "ok"}

    def status() -> dict[str, object]:
        return {**state, "healthy": state["healthy"]}

    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=status,
        metrics_provider=lambda: Metrics().render_prometheus(),
    )
    server.start()
    yield server, state
    server.stop()


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_health_ok(server) -> None:
    instance, _ = server
    code, body = get(f"{instance.url}/health")
    assert code == 200
    assert json.loads(body)["status"] == "ok"


def test_health_degraded_returns_503(server) -> None:
    instance, state = server
    state["healthy"] = False
    state["status"] = "stale_feed"
    code, body = get(f"{instance.url}/health")
    assert code == 503
    assert json.loads(body)["status"] == "stale_feed"


def test_metrics_endpoint(server) -> None:
    instance, _ = server
    code, body = get(f"{instance.url}/metrics")
    assert code == 200
    assert "kite_alerts_" in body or body == "\n"


def test_index_and_404(server) -> None:
    instance, _ = server
    code, body = get(f"{instance.url}/")
    assert code == 200
    assert "kite-alerts" in body
    code, _ = get(f"{instance.url}/nope")
    assert code == 404


def test_server_is_idempotent_and_stoppable() -> None:
    instance = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {"healthy": True},
        metrics_provider=lambda: "",
    )
    instance.start()
    instance.start()  # no-op
    port = instance.port
    assert port > 0
    instance.stop()
    instance.stop()  # no-op
    time.sleep(0.05)
