"""Bring-your-own-key Kite Connect flow for the dashboard.

The dashboard never collects a Zerodha password or TOTP. The user creates their
own Kite Connect app, pastes its API key and secret (write-only, stored in
``.env``), and is then sent to Zerodha's own login page. Zerodha redirects back
to this loopback app with a one-time ``request_token``; the token is exchanged
with the *same* functions the CLI uses (``cli/login_cmd.py``), so there is one
implementation of the login, not two.

CSRF defence: ``POST /api/kite/login`` creates a random state value, keeps it in
memory for at most ten minutes and sets it as an HttpOnly cookie. The callback
accepts only a request that carries that cookie (or an explicit matching
``state`` parameter) while an attempt is pending, and consumes the attempt. A
cross-site page cannot read the cookie and cannot guess the value.

Read-only by construction: the only Kite client methods used are the login URL,
session generation, the profile call, one ``ltp`` probe and token invalidation.
``tests/unit/test_ui_kite_allowlist.py`` fails the build if anything else appears.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.settings import Settings
from ..core.clock import SystemClock, to_ist
from .settings_store import SettingsStore

log = logging.getLogger(__name__)

STATE_TTL_SECONDS = 600
STATE_COOKIE = "ka_connect_state"
PROFILE_TTL_SECONDS = 60
DATA_TTL_SECONDS = 60
EXPIRES_NOTE = "expires about 06:00 IST"

START_AGAIN = "Click “Connect to Zerodha” again to start a new attempt."

NO_KEY_MESSAGE = (
    "Add your Kite API key and API secret first — they come from your own app at "
    "developers.kite.trade."
)
NO_SECRET_MESSAGE = "Your Kite API secret is missing. Add it in step 3, then try again."
STATE_MISSING = (
    "This login did not start from this dashboard, or the attempt timed out. " + START_AGAIN
)
STATE_EXPIRED = (
    "The login attempt took longer than ten minutes, so it was cancelled for safety. " + START_AGAIN
)
TOKEN_MISSING = "No request_token arrived. Paste it below, or click Connect again."
TOKEN_REJECTED = (
    "Zerodha rejected that request token. It is single-use and short-lived — " + START_AGAIN
)
UNREACHABLE = "Kite is unreachable right now. Check your internet connection and try again."
CANCELLED = "The Zerodha login was cancelled before it finished."


class ConnectError(Exception):
    """A user-facing failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StartResult:
    #: Relative path that redirects to Zerodha. The absolute login URL contains
    #: the API key, so it is never serialised into an API response: the browser
    #: is sent through /kite/start instead (Kite requires the key in that URL).
    start_url: str
    redirect_url: str
    state: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class SessionInfo:
    status: str
    connected: bool
    name: str = ""
    message: str = ""
    expires_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "connected": self.connected,
            "name": self.name,
            "message": self.message,
            "expires_note": self.expires_note,
            "expires_about": EXPIRES_NOTE,
        }


@dataclass(frozen=True, slots=True)
class ConnectResult:
    ok: bool
    code: str
    message: str
    session: SessionInfo | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "session": self.session.as_dict() if self.session else None,
        }


@dataclass(frozen=True, slots=True)
class DataSource:
    id: str
    label: str
    available: bool
    reason: str
    badge: str = ""
    enabled: bool = True
    notice: str = ""
    supports_oi: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "available": self.available,
            "reason": self.reason,
            "badge": self.badge,
            "enabled": self.enabled,
            "notice": self.notice,
            "supports_oi": self.supports_oi,
        }


@dataclass
class _Pending:
    state: str
    created: float
    url: str


def default_kite_factory(api_key: str, access_token: str = "") -> Any:
    """Build a real Kite client (imported lazily so tests never need the network)."""
    from kiteconnect import KiteConnect

    client = KiteConnect(api_key=api_key)
    if access_token:
        client.set_access_token(access_token)
    return client


def _classify(exc: BaseException) -> str:
    """Map a Kite exception to: token, permission, rate, network, other."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    code = getattr(exc, "code", None)
    if "token" in name or "token" in text:
        return "token"
    if "permission" in name or "permission" in text:
        return "permission"
    if "network" in name or "connection" in text or "timed out" in text:
        return "network"
    if code == 429 or "too many" in text or "rate" in text:
        return "rate"
    return "other"


@dataclass
class KiteConnectService:
    """Owns the pending login attempt, the session view and the data probe."""

    settings_store: SettingsStore
    settings_provider: Callable[[], Settings | None]
    port: int = 8765
    on_change: Callable[[], None] = lambda: None
    kite_factory: Callable[[str, str], Any] = default_kite_factory
    monotonic: Callable[[], float] = time.monotonic
    wall_clock: SystemClock = field(default_factory=SystemClock)
    state_ttl: float = STATE_TTL_SECONDS
    feed_provider: Callable[[], Any] | None = None
    _pending: _Pending | None = field(default=None, init=False)
    _profile: tuple[str, float, SessionInfo] | None = field(default=None, init=False)
    _data: tuple[str, float, tuple[DataSource, ...]] | None = field(default=None, init=False)

    # ------------------------------------------------------------------- helpers
    @property
    def env_file(self) -> Path:
        return self.settings_store.path

    @property
    def redirect_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    def _client(self, settings: Settings, token: str = "") -> Any:
        return self.kite_factory(settings.kite.api_key, token)

    def _now_label(self) -> str:
        return to_ist(self.wall_clock.now()).strftime("%H:%M:%S")

    # --------------------------------------------------------------------- login
    def start(self) -> StartResult:
        settings = self.settings_provider()
        if settings is None:
            raise ConnectError("settings", "Your settings could not be read.")
        if not settings.kite.api_key:
            raise ConnectError("missing_key", NO_KEY_MESSAGE)
        if not settings.kite.api_secret.get_secret_value():
            raise ConnectError("missing_secret", NO_SECRET_MESSAGE)

        try:
            url = str(self._client(settings).login_url())
        except Exception:
            log.warning("could not build the Kite login URL")
            raise ConnectError("unreachable", UNREACHABLE) from None

        state = secrets.token_urlsafe(32)
        self._pending = _Pending(state=state, created=self.monotonic(), url=url)
        return StartResult(
            start_url="/kite/start",
            redirect_url=self.redirect_url,
            state=state,
            expires_in=int(self.state_ttl),
        )

    def pending_login_url(self) -> str | None:
        """The Zerodha URL for ``/kite/start``, or ``None`` when no attempt is live."""
        pending = self._pending
        if pending is None:
            return None
        if self.monotonic() - pending.created > self.state_ttl:
            self._pending = None
            return None
        return pending.url

    def _validate_state(self, state: str) -> None:
        pending = self._pending
        if pending is None:
            raise ConnectError("no_attempt", STATE_MISSING)
        age = self.monotonic() - pending.created
        if age > self.state_ttl:
            self._pending = None
            raise ConnectError("state_expired", STATE_EXPIRED)
        if not state or not secrets.compare_digest(state, pending.state):
            raise ConnectError("bad_state", STATE_MISSING)

    def finish(self, request_token: str, state: str) -> ConnectResult:
        """Exchange a one-time request token and save the daily access token."""
        settings = self.settings_provider()
        if settings is None:
            raise ConnectError("settings", "Your settings could not be read.")
        self._validate_state(state)

        from ..cli.login_cmd import extract_request_token, write_access_token

        token = extract_request_token(request_token or "")
        if not token:
            raise ConnectError("missing_token", TOKEN_MISSING)
        secret = settings.kite.api_secret.get_secret_value()
        if not settings.kite.api_key or not secret:
            raise ConnectError("missing_key", NO_KEY_MESSAGE)

        try:
            data = self._client(settings).generate_session(token, api_secret=secret)
        except Exception as exc:
            kind = _classify(exc)
            # Never put the exception text (or the token) in the response.
            log.warning("Kite session exchange failed (%s)", kind)
            self._pending = None
            if kind == "network":
                raise ConnectError("unreachable", UNREACHABLE) from None
            raise ConnectError("exchange_failed", TOKEN_REJECTED) from None

        access_token = str((data or {}).get("access_token") or "")
        if not access_token:
            self._pending = None
            raise ConnectError("exchange_failed", TOKEN_REJECTED)

        write_access_token(self.env_file, access_token)
        self._pending = None
        self._profile = None
        self._data = None
        self.on_change()
        return ConnectResult(
            ok=True,
            code="connected",
            message="Connected to Zerodha. The access token is valid until about 06:00 IST.",
            session=self.session(force=True),
        )

    def cancel_pending(self) -> None:
        self._pending = None

    # ------------------------------------------------------------------- session
    def session(self, *, force: bool = False) -> SessionInfo:
        settings = self.settings_provider()
        if settings is None:
            return SessionInfo(
                status="not_connected",
                connected=False,
                message="Your settings could not be read, so the dashboard cannot connect.",
            )
        token = settings.kite.token
        if not token:
            return SessionInfo(
                status="not_connected",
                connected=False,
                message="Not connected. Add your own Kite API key and secret, then connect.",
                expires_note=EXPIRES_NOTE,
            )
        now = self.monotonic()
        if not force and self._profile and self._profile[0] == token:
            age = now - self._profile[1]
            if age < PROFILE_TTL_SECONDS:
                return self._profile[2]

        try:
            profile = self._client(settings, token).profile() or {}
        except Exception as exc:
            kind = _classify(exc)
            if kind == "token":
                info = SessionInfo(
                    status="expired",
                    connected=False,
                    message="Your Kite session expired. Reconnect to keep using live data.",
                    expires_note=EXPIRES_NOTE,
                )
            elif kind == "network":
                info = SessionInfo(
                    status="unreachable",
                    connected=False,
                    message="Kite is unreachable, so the session cannot be checked right now.",
                    expires_note=EXPIRES_NOTE,
                )
            else:
                info = SessionInfo(
                    status="not_connected",
                    connected=False,
                    message="Kite could not confirm this session. Reconnect to renew it.",
                    expires_note=EXPIRES_NOTE,
                )
            log.warning("Kite profile check failed (%s)", kind)
            self._profile = (token, now, info)
            return info

        name = str(profile.get("user_name") or profile.get("user_id") or "your Zerodha account")
        info = SessionInfo(
            status="connected",
            connected=True,
            name=name,
            message=f"Connected as {name}, {EXPIRES_NOTE}.",
            expires_note=EXPIRES_NOTE,
        )
        self._profile = (token, now, info)
        return info

    def disconnect(self) -> ConnectResult:
        settings = self.settings_provider()
        if settings is None or not settings.kite.token:
            return ConnectResult(ok=True, code="disconnected", message="Already disconnected.")
        token = settings.kite.token
        try:
            self._client(settings, token).invalidate_access_token()
        except Exception as exc:  # best effort; clearing locally always succeeds
            log.info("Kite token invalidation failed (%s)", _classify(exc))
        from ..cli.login_cmd import write_access_token

        write_access_token(self.env_file, "")
        self._pending = None
        self._profile = None
        self._data = None
        self.on_change()
        return ConnectResult(
            ok=True,
            code="disconnected",
            message="Disconnected. The saved access token was cleared from .env.",
        )

    # --------------------------------------------------------------- data sources
    def _probe_live(self, settings: Settings, token: str) -> tuple[bool, str]:
        try:
            data = self._client(settings, token).ltp(["NSE:NIFTY 50"])
        except Exception as exc:
            kind = _classify(exc)
            log.info("market-data probe failed (%s)", kind)
            if kind == "permission":
                return (
                    False,
                    "Your Kite plan has no market data, so the live chart is unavailable; "
                    "the delayed chart will be used.",
                )
            if kind == "token":
                return False, "Token expired. Reconnect to check your Kite data plan."
            if kind == "rate":
                return False, "Kite is rate limiting this account; try again in a minute."
            if kind == "network":
                return False, "Kite is unreachable, so live data cannot be checked."
            return False, "Kite could not confirm live market data for this account."
        if data:
            return True, "Live data available from your Kite account."
        return False, "Kite returned no data for the probe, so live data is unavailable."

    def data_sources(self, *, force: bool = False) -> dict[str, Any]:
        settings = self.settings_provider()
        token = settings.kite.token if settings is not None else ""
        now = self.monotonic()
        feed = self.feed_provider() if self.feed_provider is not None else None
        if (
            not force
            and self._data is not None
            and self._data[0] == token
            and now - self._data[1] < DATA_TTL_SECONDS
        ):
            sources = self._data[2]
        else:
            if feed is not None and getattr(feed, "is_simulated", False):
                live = DataSource(
                    "kite_live",
                    "Kite live (simulated)",
                    True,
                    "Simulated market data feed. Not real market data.",
                    badge="SIMULATED",
                    enabled=True,
                    notice="SIMULATED, not real market data",
                    supports_oi=True,
                )
            elif not token:
                live = DataSource(
                    "kite_live",
                    "Kite live",
                    False,
                    "Connect to Zerodha to use live Kite market data.",
                    badge="LIVE",
                    enabled=False,
                    notice=(
                        "Live Kite feed arrives in a later update (C4)."
                        if feed is None
                        else (
                            "Connect to your Zerodha account with a market data "
                            "plan to use live data."
                        )
                    ),
                    supports_oi=True,
                )
            else:
                assert settings is not None
                available, reason = self._probe_live(settings, token)
                if feed is None:
                    notice = "Live Kite feed arrives in a later update (C4)."
                    enabled = False
                else:
                    feed_status = feed.status()
                    enabled = available
                    if available:
                        notice = "Live data available from your Kite account."
                        if feed_status and feed_status.state == "live":
                            notice = "Streaming live ticks from Kite."
                        elif feed_status and feed_status.state == "connecting":
                            notice = "Connecting to Kite live ticker..."
                        elif feed_status and feed_status.state == "stale":
                            notice = feed_status.reason or "Live feed is stale."
                    else:
                        notice = (
                            f"{reason} Live Kite feed arrives in a later update (C4) "
                            "if not connected."
                        )
                live = DataSource(
                    "kite_live",
                    "Kite live",
                    available if feed is not None else False,
                    reason,
                    badge="LIVE",
                    enabled=enabled,
                    notice=notice,
                    supports_oi=True,
                )
            sources = (
                DataSource(
                    "nse_delayed",
                    "Delayed NSE",
                    True,
                    "Delayed NSE quotes need no login and are used when live data is unavailable.",
                    badge="DELAYED",
                    notice="Unofficial delayed data from a free public source. Not live.",
                ),
                live,
                DataSource(
                    "crypto_showcase",
                    "Crypto showcase",
                    True,
                    "A built-in demonstration feed that needs no account.",
                    badge="SHOWCASE",
                    notice="Crypto showcase is not NSE data.",
                ),
                DataSource(
                    "demo",
                    "Demo replay",
                    True,
                    "The offline practice replay; no account, no internet.",
                    badge="DEMO",
                    notice="A made-up day for practice. Nothing is saved.",
                ),
            )
            self._data = (token, now, sources)
        is_sim = bool(feed and getattr(feed, "is_simulated", False))
        return {
            "sources": [source.as_dict() for source in sources],
            "connected": bool(token) and not is_sim,
            "checked_at": self._now_label(),
        }


__all__ = [
    "DATA_TTL_SECONDS",
    "EXPIRES_NOTE",
    "STATE_COOKIE",
    "STATE_TTL_SECONDS",
    "ConnectError",
    "ConnectResult",
    "KiteConnectService",
    "SessionInfo",
    "StartResult",
    "default_kite_factory",
]
