"""N3 — the SSE loop in ``ui/app.py:chart_stream`` had no tests at all.

The F17 fix (``asyncio.to_thread(client.get, ...)`` and ``admit_sse_client``)
already exists on main, so these tests pass on main; they exist so the loop
cannot regress silently. They are driven through the raw ASGI interface with a
strict fake socket, which makes every interleaving deterministic: no sleeps,
no real network, and the disconnect arrives exactly when the test decides.

Covered here:

* the event loop stays responsive while one client is slow (and ``get`` never
  runs on the event-loop thread);
* the client cap is enforced under the lock (barrier-synchronised threads);
* a disconnect unregisters and closes the client (no leaked registration);
* streams do not leak threads (sequential streams reuse one worker).
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fakes.fake_kite import FakeTicker
from kite_alerts.ui.app import build_context, create_app
from kite_alerts.ui.live_feed import LiveFeedService, SSEClient
from unit.ui_support import BASE_URL

MAX_SSE_CLIENTS = 50

HOST_HEADERS = [
    (b"host", b"127.0.0.1:8765"),
    (b"accept", b"text/event-stream"),
    (b"user-agent", b"strict-fake-socket"),
]


class FakeSocket:
    """A strict ASGI client socket: the test controls every message."""

    def __init__(self) -> None:
        self.to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.to_client: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def receive(self) -> dict[str, Any]:
        return await self.to_app.get()

    async def send(self, message: dict[str, Any]) -> None:
        await self.to_client.put(message)

    async def send_request(self) -> None:
        await self.to_app.put({"type": "http.request", "body": b"", "more_body": False})

    async def disconnect(self) -> None:
        await self.to_app.put({"type": "http.disconnect"})


def _scope(path: str, query: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": HOST_HEADERS,
        "client": ("127.0.0.1", 51000),
        "server": ("127.0.0.1", 8765),
    }


async def _start_stream(
    app: Any, query: str = "source=kite_live&symbol=NSE:TEST&interval=1"
) -> tuple[FakeSocket, asyncio.Task[None]]:
    socket = FakeSocket()
    await socket.send_request()
    task = asyncio.create_task(app(_scope("/api/chart/stream", query), socket.receive, socket.send))
    return socket, task


async def _read_body(socket: FakeSocket, *, chunks: int = 1) -> bytes:
    """Read ``chunks`` body messages (SSE events arrive as separate chunks)."""
    parts: list[bytes] = []
    while len(parts) < chunks:
        message = await asyncio.wait_for(socket.to_client.get(), 5)
        if message["type"] == "http.response.body":
            parts.append(message.get("body", b""))
    return b"".join(parts)


async def _first_event(socket: FakeSocket) -> tuple[str, dict[str, Any]]:
    raw = await _read_body(socket)
    lines = raw.decode().splitlines()
    event = lines[0].removeprefix("event: ").strip()
    data = json.loads(lines[1].removeprefix("data: "))
    return event, data


async def _get_json(app: Any, path: str) -> tuple[int, Any]:
    socket = FakeSocket()
    await socket.send_request()
    task = asyncio.create_task(app(_scope(path, ""), socket.receive, socket.send))
    start = await asyncio.wait_for(socket.to_client.get(), 5)
    status = start["status"]
    body = await asyncio.wait_for(socket.to_client.get(), 5)
    await asyncio.wait_for(task, 5)
    return status, json.loads(body.get("body", b"{}"))


def _context_for(tmp_path: Path) -> Any:
    """A context whose feed exists but never starts: no ticker threads at all."""
    FakeTicker.reset()
    return build_context(env_file=tmp_path / "missing.env")


class TestStreamResponsiveness:
    def test_get_never_runs_on_the_event_loop_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            context = _context_for(tmp_path)
            app = create_app(context)

            threads_seen: list[str] = []
            original_get = SSEClient.get

            def recording_get(self: SSEClient, timeout: float = 15.0) -> Any:
                threads_seen.append(threading.current_thread().name)
                return original_get(self, timeout)

            monkeypatch.setattr(SSEClient, "get", recording_get)

            socket, task = await _start_stream(app)
            event, data = await _first_event(socket)
            assert event == "status"
            assert data["state"] in {"disconnected", "connecting"}

            # The stream is now idle (no events pending) and the strict fake
            # socket has not sent a disconnect. The event loop must still be
            # able to serve another request end-to-end.
            status, body = await _get_json(app, "/api/status")
            assert status == 200
            assert body["alerts_only"] is True

            # And the task is still streaming, not finished.
            assert not task.done()

            await socket.disconnect()
            await asyncio.wait_for(task, 5)

            loop_thread = threading.current_thread().name
            assert threads_seen, "the stream never read from its client"
            assert loop_thread not in threads_seen, (
                "SSEClient.get blocked the event loop (the pre-F17 behaviour)"
            )

        asyncio.run(scenario())


class TestStreamCap:
    def test_admit_enforces_the_cap_under_concurrency(self) -> None:
        feed = LiveFeedService(
            ticker_factory=lambda key, token: FakeTicker(key, token),
            kite_factory=lambda: object(),
        )
        workers = 60
        barrier = threading.Barrier(workers)
        accepted: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()  # every thread releases at once: a stale len() loses
            result = feed.admit_sse_client(SSEClient("NSE:TEST"))
            with lock:
                accepted.append(result)

        threads = [threading.Thread(target=worker, name=f"adm-{n}") for n in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()

        assert sum(accepted) == MAX_SSE_CLIENTS
        assert len(feed._sse_clients) == MAX_SSE_CLIENTS

    def test_the_51st_stream_is_rejected_with_429(self, tmp_path: Path) -> None:
        context = _context_for(tmp_path)
        for _ in range(MAX_SSE_CLIENTS):
            assert context.feed.admit_sse_client(SSEClient("NSE:TEST")) is True
        client = TestClient(create_app(context), base_url=BASE_URL)
        with client:
            response = client.get("/api/chart/stream?source=kite_live&symbol=NSE:TEST")
        assert response.status_code == 429


class TestStreamDisconnect:
    def test_disconnect_unregisters_and_closes_the_client(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            context = _context_for(tmp_path)
            feed = context.feed
            app = create_app(context)
            unregistered: list[Any] = []
            original = feed.unregister_sse_client

            def recording(client: Any) -> None:
                unregistered.append(client)
                original(client)

            feed.unregister_sse_client = recording  # type: ignore[method-assign]

            socket, task = await _start_stream(app)
            await _first_event(socket)

            await socket.disconnect()
            await asyncio.wait_for(task, 5)

            assert task.done()
            assert len(unregistered) == 1, "the stream generator's finally must run"
            assert feed._sse_clients == []
            assert feed.admit_sse_client(SSEClient("NSE:TEST")) is True, (
                "the disconnecting client must free its slot"
            )

        asyncio.run(scenario())


class TestNoThreadLeak:
    def test_sequential_streams_reuse_the_executor(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            context = _context_for(tmp_path)
            app = create_app(context)

            for _ in range(3):
                socket, task = await _start_stream(app)
                await _first_event(socket)
                await socket.disconnect()
                await asyncio.wait_for(task, 5)
            after_first_batch = threading.active_count()

            for _ in range(3):
                socket, task = await _start_stream(app)
                await _first_event(socket)
                await socket.disconnect()
                await asyncio.wait_for(task, 5)
            after_second_batch = threading.active_count()

            assert after_second_batch <= after_first_batch, (
                "every stream leaked threads (executor workers must be reused)"
            )

        asyncio.run(scenario())
