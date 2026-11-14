"""Start the local dashboard.

Deliberately small: validate the bind address (loopback only, with a clear
message), build the app, optionally open the browser once the port answers, then
hand control to uvicorn.
"""

from __future__ import annotations

import socket
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError
from .app import build_context, create_app
from .security import validate_bind_host

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

INVALID_PORT_MESSAGE = "Port {port} is not valid. Use a number between 1024 and 65535, e.g. 8765."


def validate_port(port: int) -> int:
    if not 1 <= int(port) <= 65535:
        raise ConfigError(INVALID_PORT_MESSAGE.format(port=port))
    return int(port)


def dashboard_url(host: str, port: int) -> str:
    shown = "127.0.0.1" if host in ("localhost", "::1") else host
    return f"http://{shown}:{port}/"


def _is_listening(host: str, port: int, *, timeout: float = 0.25) -> bool:
    target = "127.0.0.1" if host == "localhost" else host
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except OSError:
        return False


def _open_when_ready(
    url: str,
    browser_open: Callable[[str], Any],
    *,
    host: str,
    port: int,
    timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    listening: Callable[..., bool] = _is_listening,
) -> None:
    """Open the browser only once the dashboard answers (avoids a refused page)."""
    deadline = timeout
    while deadline > 0:
        if listening(host, port):
            try:
                browser_open(url)
            except Exception:  # opening a browser is a convenience, never fatal
                return
            return
        sleep(0.1)
        deadline -= 0.1


def run_ui(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
    env_file: str | Path = ".env",
    db_path: str | Path | None = None,
    browser_open: Callable[[str], Any] = webbrowser.open,
) -> int:
    """Run the dashboard until Ctrl-C. Raises ``ConfigError`` for a bad bind/port."""
    host = validate_bind_host(host)
    port = validate_port(port)
    context = build_context(env_file=env_file, db_path=db_path)
    app = create_app(context)
    url = dashboard_url(host, port)

    print(
        f"kite-alerts dashboard: {url}\n"
        "Alerts only — this dashboard reads history and replays a demo; "
        "it never places orders.\n"
        "Press Ctrl-C in this window to stop the dashboard.",
        flush=True,
    )
    if context.settings_error:
        print(
            "Note: your settings could not be read, so the demo still works but live "
            "status and test alerts are limited. Run `kite-alerts config-check` for details.",
            flush=True,
        )
    if context.settings is not None:
        print(f"Reading alert history from: {context.history.path}", flush=True)

    if open_browser:
        threading.Thread(
            target=_open_when_ready,
            args=(url, browser_open),
            kwargs={"host": host, "port": port},
            name="kite-alerts-open-browser",
            daemon=True,
        ).start()

    import uvicorn  # imported here so the headless bot never needs it

    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
    return 0
