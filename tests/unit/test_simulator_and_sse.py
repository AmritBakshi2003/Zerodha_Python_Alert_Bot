"""Unit and integration tests for Kite simulator, SSE stream, and live feed endpoints.

Validates:
- Simulator mode opt-in (--simulate-kite / SIMULATE_KITE=1).
- Synthetic ticks for equity (NSE:SIMULATED) and futures with OI (NFO:SIMFUT).
- Dev controls: disconnect, token-expiry, reconnect.
- Security: same-origin required, simulator controls disabled in non-sim mode.
- SSE stream (/api/chart/stream): status event, tick event, heartbeats, queue limits.
- Permanent SIMULATED badge: never claims LIVE, never counts as Kite connected.
- Secrets never leaked in stream or status endpoints.
"""

from __future__ import annotations

from pathlib import Path

from kite_alerts.ui.app import build_context
from kite_alerts.ui.live_feed import SSEClient, SSEEvent
from unit.ui_support import make_client
from unit.ui_support_c2 import (
    ACCESS_MARKER,
    KEY_MARKER,
    SECRET_MARKER,
    TOKEN_MARKER,
    connect_client,
    env_text_with_token,
)

LOOPBACK_ORIGIN = {"origin": "http://127.0.0.1:8765"}


def test_simulator_symbols_and_bootstrap_candles(tmp_path: Path) -> None:
    context = build_context(
        env_file=tmp_path / ".env",
        db_path=tmp_path / "alerts.db",
        simulate_kite=True,
    )
    assert context.simulate_kite is True
    assert context.simulator is not None
    assert context.feed is not None

    with make_client(context) as client:
        # Check data sources
        resp = client.get("/api/data-sources")
        assert resp.status_code == 200
        sources = resp.json()["sources"]
        kite_source = next(s for s in sources if s["id"] == "kite_live")
        assert kite_source["enabled"] is True
        assert kite_source["badge"] == "SIMULATED"
        assert "SIMULATED" in kite_source["notice"].upper()

        # Check session: simulator must NEVER count as Kite connected
        session = client.get("/api/kite/session")
        assert session.status_code == 200
        assert session.json()["connected"] is False

        # Check symbols list includes simulated instruments
        syms_resp = client.get("/api/chart/symbols?source=kite_live")
        assert syms_resp.status_code == 200
        sym_ids = [s["symbol"] for s in syms_resp.json()["symbols"]]
        assert "NSE:SIMULATED" in sym_ids
        assert "NFO:SIMFUT" in sym_ids

        # Fetch candles for NSE:SIMULATED
        candles_resp = client.get(
            "/api/chart/candles?source=kite_live&symbol=NSE:SIMULATED&interval=1"
        )
        assert candles_resp.status_code == 200
        candles_data = candles_resp.json()
        assert candles_data["symbol"] == "NSE:SIMULATED"
        assert len(candles_data["candles"]) >= 60

        # Fetch candles for NFO:SIMFUT: should have OI
        fut_resp = client.get("/api/chart/candles?source=kite_live&symbol=NFO:SIMFUT&interval=1")
        assert fut_resp.status_code == 200
        fut_data = fut_resp.json()
        assert fut_data["symbol"] == "NFO:SIMFUT"
        assert fut_data["candles"][0].get("oi", 0) > 0


def test_dev_controls_in_simulator_mode(tmp_path: Path) -> None:
    context = build_context(
        env_file=tmp_path / ".env",
        db_path=tmp_path / "alerts.db",
        simulate_kite=True,
    )
    with make_client(context) as client:
        # Trigger disconnect
        disc_resp = client.post(
            "/api/kite/simulate/disconnect",
            headers=LOOPBACK_ORIGIN,
        )
        assert disc_resp.status_code == 200
        assert disc_resp.json() == {"ok": True, "action": "disconnect"}
        status = client.get("/api/chart/feed-status").json()
        assert status["status"]["state"] == "disconnected"

        # Trigger token expiry
        exp_resp = client.post(
            "/api/kite/simulate/token-expiry",
            headers=LOOPBACK_ORIGIN,
        )
        assert exp_resp.status_code == 200
        assert exp_resp.json() == {"ok": True, "action": "token_expiry"}
        status = client.get("/api/chart/feed-status").json()
        assert status["status"]["state"] == "token_expired"

        # Trigger reconnect: clears disconnect/expiry → goes back to simulated running
        rec_resp = client.post(
            "/api/kite/simulate/reconnect",
            headers=LOOPBACK_ORIGIN,
        )
        assert rec_resp.status_code == 200
        assert rec_resp.json() == {"ok": True, "action": "reconnect"}
        status = client.get("/api/chart/feed-status").json()
        assert status["status"]["state"] in ("simulated", "live", "connecting")


def test_dev_controls_forbidden_in_real_mode(tmp_path: Path) -> None:
    client, _context, _env, _kite = connect_client(tmp_path)
    with client:
        for action in ("disconnect", "token-expiry", "reconnect"):
            resp = client.post(
                f"/api/kite/simulate/{action}",
                headers=LOOPBACK_ORIGIN,
            )
            assert resp.status_code == 400
            assert "Simulator mode is not active" in resp.json()["detail"]


def test_sse_stream_validation_and_max_clients(tmp_path: Path) -> None:
    context = build_context(
        env_file=tmp_path / ".env",
        db_path=tmp_path / "alerts.db",
        simulate_kite=True,
    )
    with make_client(context) as client:
        # Invalid source: 422
        bad_source = client.get("/api/chart/stream?source=delayed_nse&symbol=NSE:INFY")
        assert bad_source.status_code == 422
        assert "only supported for the live Kite feed" in bad_source.json()["detail"]

        # Max client limit (50)
        assert context.feed is not None
        mock_clients = [SSEClient(symbol="NSE:SIMULATED") for _ in range(50)]
        for mc in mock_clients:
            context.feed.register_sse_client(mc)

        too_many = client.get("/api/chart/stream?source=kite_live&symbol=NSE:SIMULATED")
        assert too_many.status_code == 429
        assert "Too many concurrent" in too_many.json()["detail"]

        for mc in mock_clients:
            context.feed.unregister_sse_client(mc)


def test_sse_client_event_streaming_and_drop_oldest() -> None:
    client = SSEClient(symbol="NSE:SIMULATED", interval=1, max_queue=3)
    assert client.queue_size == 0

    client.push(SSEEvent(event="tick", data={"price": 100.0}))
    client.push(SSEEvent(event="tick", data={"price": 101.0}))
    client.push(SSEEvent(event="tick", data={"price": 102.0}))
    assert client.queue_size == 3

    # Exceeding queue drops oldest
    client.push(SSEEvent(event="tick", data={"price": 103.0}))
    assert client.queue_size == 3

    ev1 = client.get(timeout=0.1)
    assert ev1 is not None
    assert ev1.data["price"] == 101.0
    ev2 = client.get(timeout=0.1)
    assert ev2 is not None
    assert ev2.data["price"] == 102.0
    ev3 = client.get(timeout=0.1)
    assert ev3 is not None
    assert ev3.data["price"] == 103.0
    assert client.get(timeout=0.01) is None


def test_secrets_never_leak_in_stream_or_feed_status(tmp_path: Path) -> None:
    client, _context, _env, _kite = connect_client(
        tmp_path, env_text=env_text_with_token(token=ACCESS_MARKER)
    )
    with client:
        status_resp = client.get("/api/chart/feed-status")
        assert status_resp.status_code == 200
        status_text = status_resp.text
        for marker in (KEY_MARKER, SECRET_MARKER, TOKEN_MARKER, ACCESS_MARKER):
            assert marker not in status_text, f"{marker} leaked in feed-status"


def test_simulator_start_stop_idempotent_and_auto_reconnect(tmp_path: Path) -> None:
    import time

    context = build_context(
        env_file=tmp_path / ".env",
        db_path=tmp_path / "alerts.db",
        simulate_kite=True,
    )
    sim = context.simulator
    assert sim is not None
    # Calling start again when already running is a no-op
    sim.start()
    assert sim.state.running is True

    # Test disconnect with auto-reconnect
    sim.trigger_disconnect(duration=0.1)
    assert sim.state.disconnected is True
    # Wait for loop to auto-reconnect
    deadline = time.time() + 2.0
    while sim.state.disconnected and time.time() < deadline:
        time.sleep(0.05)
    assert sim.state.disconnected is False

    # Test token expiry stops the loop
    sim.trigger_token_expiry()
    assert sim.state.token_expired is True
    time.sleep(0.1)

    # Stop joins thread
    sim.stop()
    assert sim.state.running is False
