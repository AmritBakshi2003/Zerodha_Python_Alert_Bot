"""``/health`` and ``/metrics`` on a small stdlib HTTP server.

Binds loopback by default (config validation refuses a public bind unless it is
explicitly allowed). ``/health`` returns 503 when the bot is unhealthy so a
supervisor or uptime monitor can act on it.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger(__name__)

StatusProvider = Callable[[], dict[str, Any]]
MetricsProvider = Callable[[], str]


class HealthServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        status_provider: StatusProvider,
        metrics_provider: MetricsProvider,
    ) -> None:
        self._host = host
        self._port = port
        self._status_provider = status_provider
        self._metrics_provider = metrics_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """Actual bound port (useful with ``port=0`` in tests)."""
        if self._server is not None:
            return int(self._server.server_address[1])
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}"

    def start(self) -> None:
        if self._server is not None:
            return
        status_provider = self._status_provider
        metrics_provider = self._metrics_provider

        class Handler(BaseHTTPRequestHandler):
            server_version = "kite-alerts/1"
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                if path == "/health":
                    payload = status_provider()
                    body = json.dumps(payload, default=str).encode()
                    code = 200 if payload.get("healthy", False) else 503
                    self._respond(code, "application/json", body)
                elif path == "/metrics":
                    self._respond(200, "text/plain; version=0.0.4", metrics_provider().encode())
                elif path in ("/", "/status"):
                    self._respond(
                        200,
                        "application/json",
                        json.dumps(
                            {"service": "kite-alerts", "endpoints": ["/health", "/metrics"]}
                        ).encode(),
                    )
                else:
                    self._respond(404, "application/json", b'{"error":"not found"}')

            def _respond(self, code: int, content_type: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                    pass

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("http: " + fmt, *args)

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="kite-alerts-http", daemon=True
        )
        self._thread.start()
        log.info("health/metrics listening on %s", self.url)

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # pragma: no cover - shutdown path
                log.debug("http shutdown raised", exc_info=True)
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
