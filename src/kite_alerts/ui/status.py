"""Is the bot running? Ask its existing ``/health`` endpoint.

The dashboard never starts or stops the bot in C1; it only reports whether the
bot's own health server answers. No credentials are involved: ``/health`` is a
plain loopback endpoint that the headless runner already exposes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import requests

from ..config.settings import Settings
from ..core.clock import Clock, SystemClock, to_ist

log = logging.getLogger(__name__)

#: ``(status_code, payload)`` or ``None`` when nothing answered.
Probe = Callable[[str, float], "tuple[int, dict[str, Any]] | None"]

PROBE_TIMEOUT_SECONDS = 0.7

NOT_RUNNING_DETAIL = (
    "Start the bot in another window with `kite-alerts run`. "
    "This dashboard only watches it — it never starts trading or sends orders."
)
DISABLED_DETAIL = (
    "The bot's health check is switched off (HTTP_ENABLED=false). "
    "Set HTTP_ENABLED=true in .env and restart the bot to see its status here."
)
NO_SETTINGS_DETAIL = (
    "The dashboard could not read your settings, so it cannot find the bot. "
    "Fix the .env message below and restart the dashboard."
)


def http_probe(
    url: str, timeout: float = PROBE_TIMEOUT_SECONDS
) -> tuple[int, dict[str, Any]] | None:
    """GET ``url`` and return its JSON body; ``None`` when nothing is listening."""
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException:
        return None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}


@dataclass(frozen=True, slots=True)
class BotStatus:
    """A plain-language health snapshot for the dashboard's status card."""

    running: bool
    label: str
    detail: str
    endpoint: str
    health_status: str | None = None
    checked_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class StatusService:
    def __init__(
        self,
        settings: Settings | None,
        *,
        probe: Probe = http_probe,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._probe = probe
        self._clock = clock or SystemClock()

    def endpoint(self) -> str:
        if self._settings is None:
            return ""
        observability = self._settings.observability
        host = observability.http_host
        if host not in ("127.0.0.1", "localhost"):
            # The health server may be bound to any loopback name; probe it there.
            host = "127.0.0.1"
        host = "127.0.0.1" if host == "localhost" else host
        return f"http://{host}:{observability.http_port}/health"

    def snapshot(self) -> BotStatus:
        now = to_ist(self._clock.now()).strftime("%H:%M:%S")
        if self._settings is None:
            return BotStatus(
                running=False,
                label="Bot not running",
                detail=NO_SETTINGS_DETAIL,
                endpoint="",
                checked_at=now,
            )
        if not self._settings.observability.http_enabled:
            return BotStatus(
                running=False,
                label="Bot not running",
                detail=DISABLED_DETAIL,
                endpoint="",
                checked_at=now,
            )

        url = self.endpoint()
        try:
            result = self._probe(url, PROBE_TIMEOUT_SECONDS)
        except Exception:  # a status probe must never take the dashboard down
            log.debug("health probe raised", exc_info=True)
            result = None

        if result is None:
            return BotStatus(
                running=False,
                label="Bot not running",
                detail=NOT_RUNNING_DETAIL,
                endpoint=url,
                checked_at=now,
            )

        code, payload = result
        health_status = str(payload.get("status") or ("ok" if code == 200 else "unknown"))
        if code == 200 and health_status == "ok":
            detail = "Connected to the bot's health check. Market data looks healthy."
        elif code == 200:
            detail = f"The bot is running and reports '{health_status}'."
        else:
            detail = (
                f"The bot is running but its health check returns '{health_status}'. "
                "Check the window running `kite-alerts run` for the reason."
            )
        return BotStatus(
            running=True,
            label="Bot running",
            detail=detail,
            endpoint=url,
            health_status=health_status,
            checked_at=now,
        )
