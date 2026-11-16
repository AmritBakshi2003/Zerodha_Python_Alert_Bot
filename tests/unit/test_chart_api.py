"""Read-only chart endpoints: validation, SSRF, staleness and alert isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from unit.ui_support_c3 import make_chart_client, make_chart_context


class TestEndpoints:
    def test_symbols_candles_and_live(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path, clock_value="2026-11-16 14:20")
        with make_chart_client(context) as client:
            symbols = client.get("/api/chart/symbols", params={"source": "nse_delayed"})
            assert symbols.status_code == 200
            body = symbols.json()
            assert body["badge"] == "DELAYED"
            assert [item["symbol"] for item in body["symbols"]] == ["NSE:TEST", "NFO:FUT"]
            assert body["chart_types"][0] == "candles"
            assert body["intervals"] == [1, 3, 5, 15]

            response = client.get(
                "/api/chart/candles",
                params={"source": "nse_delayed", "symbol": "NSE:TEST", "interval": 5},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["badge"] == "DELAYED"
            assert payload["source_label"] == "Delayed NSE"
            assert payload["market"]["state"] == "open"
            assert payload["freshness"]["checked_at"]
            assert payload["signal"]["label"].startswith("Informational")

            live = client.get(
                "/api/chart/live",
                params={"source": "nse_delayed", "symbol": "NSE:TEST", "interval": 1},
            )
            assert live.status_code == 200
            assert len(live.json()["candles"]) == 2

    def test_demo_and_clock(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        with make_chart_client(context) as client:
            symbols = client.get("/api/chart/symbols", params={"source": "demo"})
            assert symbols.json()["symbols"][0]["symbol"] == "DEMO:FUT"
            candles = client.get(
                "/api/chart/candles",
                params={"source": "demo", "symbol": "DEMO:FUT", "interval": 5},
            )
            assert candles.status_code == 200
            assert candles.json()["oi"]["available"] is True
            assert "of 5 agree" in candles.json()["signal"]["summary"]
            clock = client.get("/api/chart/clock")
            assert clock.status_code == 200
            assert clock.json()["market"]["session_open"] == "09:15"

    @pytest.mark.parametrize(
        "params",
        [
            {"source": "made_up", "symbol": "NSE:TEST"},
            {"source": "nse_delayed", "symbol": "NFO:FUT"},
            {"source": "nse_delayed", "symbol": "http://evil.example/x"},
            {"source": "nse_delayed", "symbol": "NSE:TEST", "interval": 7},
            {"source": "nse_delayed", "symbol": "NSE:TEST", "limit": 5},
            {"source": "nse_delayed", "symbol": "NSE:TEST", "limit": 999},
        ],
    )
    def test_bad_parameters_are_422(self, tmp_path: Path, params) -> None:
        context, _fake = make_chart_context(tmp_path)
        with make_chart_client(context) as client:
            response = client.get("/api/chart/candles", params=params)
            assert response.status_code == 422
            body = response.json()
            assert body.get("ok") is False or "detail" in body
            assert "http" not in str(body).lower()

    def test_ssrf_attempt_never_calls_a_provider(self, tmp_path: Path) -> None:
        context, fake = make_chart_context(tmp_path)
        with make_chart_client(context) as client:
            client.get(
                "/api/chart/candles",
                params={"source": "nse_delayed", "symbol": "https://evil.example/data"},
            )
            client.get("/api/chart/symbols", params={"source": "https://evil.example"})
        assert fake.calls == []

    def test_kite_source_is_refused_without_fetching(self, tmp_path: Path) -> None:
        context, fake = make_chart_context(tmp_path)
        with make_chart_client(context) as client:
            response = client.get(
                "/api/chart/candles",
                params={"source": "kite_live", "symbol": "NSE:TEST"},
            )
        assert response.status_code == 422
        message = response.json()["message"]
        assert "later update" not in message.lower()
        assert "not available" in message.lower()
        assert fake.calls == []

    def test_stale_data_is_200_with_a_visible_mark(self, tmp_path: Path) -> None:
        context, fake = make_chart_context(tmp_path)
        fake.stale = True
        with make_chart_client(context) as client:
            response = client.get(
                "/api/chart/candles",
                params={"source": "nse_delayed", "symbol": "NSE:TEST"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["stale"] is True
        assert body["freshness"]["stale_since"]
        assert "unavailable" in body["message"].lower()

    def test_chart_routes_are_rate_limited(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        context.chart.rate_limit_per_minute = 1
        with make_chart_client(context) as client:
            assert client.get("/api/chart/clock").status_code == 200
            second = client.get("/api/chart/clock")
            assert second.status_code == 429
            assert "too often" in second.json()["detail"]


class TestWatchlistRoutes:
    def test_add_and_remove_symbol(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        with make_chart_client(context) as client:
            added = client.post(
                "/api/chart/watchlist",
                json={"source": "nse_delayed", "symbol": "NSE:INFY"},
            )
            assert added.status_code == 200
            assert any(item["symbol"] == "NSE:INFY" for item in added.json()["items"])
            removed = client.request(
                "DELETE",
                "/api/chart/watchlist",
                json={"source": "nse_delayed", "symbol": "NSE:INFY"},
            )
            assert removed.status_code == 200
            assert not any(item["symbol"] == "NSE:INFY" for item in removed.json()["items"])

    def test_cross_site_write_is_403(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        with make_chart_client(context) as client:
            response = client.post(
                "/api/chart/watchlist",
                headers={"Origin": "http://evil.example"},
                json={"source": "nse_delayed", "symbol": "NSE:INFY"},
            )
        assert response.status_code == 403

    def test_bad_watchlist_source_is_422(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        with make_chart_client(context) as client:
            response = client.post(
                "/api/chart/watchlist",
                json={"source": "crypto_showcase", "symbol": "BTC"},
            )
        assert response.status_code == 422


class TestDataSourcesAndIsolation:
    def test_data_sources_carry_c3_metadata(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        with make_chart_client(context) as client:
            response = client.get("/api/data-sources")
        assert response.status_code == 200
        sources = {source["id"]: source for source in response.json()["sources"]}
        assert sources["nse_delayed"]["badge"] == "DELAYED"
        assert sources["crypto_showcase"]["badge"] == "SHOWCASE"
        assert sources["demo"]["badge"] == "DEMO"
        assert sources["kite_live"]["enabled"] is False
        notice = sources["kite_live"]["notice"]
        assert notice
        assert "later update" not in notice.lower()
        assert sources["nse_delayed"]["supports_oi"] is False

    def test_chart_calls_never_send_alerts_or_write_history(self, tmp_path: Path) -> None:
        context, _fake = make_chart_context(tmp_path)
        before = context.history.today_counts()

        def forbidden() -> None:
            raise AssertionError("chart code must never call the test-alert sender")

        context.test_alerts.send = forbidden  # type: ignore[method-assign]
        with make_chart_client(context) as client:
            for path in (
                "/api/chart/symbols?source=demo",
                "/api/chart/candles?source=demo&symbol=DEMO:FUT&interval=5",
                "/api/chart/live?source=demo&symbol=DEMO:FUT&interval=1",
                "/api/chart/watchlist?source=demo",
                "/api/chart/clock",
            ):
                assert client.get(path).status_code == 200
        assert context.history.today_counts() == before
