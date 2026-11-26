"""Loopback enforcement and browser-hardening for the local dashboard.

The dashboard is a privileged view of a trading tool, so it is deliberately
paranoid:

* it refuses to bind anything except a loopback address (``validate_bind_host``);
* every request must carry a loopback ``Host`` header, which blocks DNS
  rebinding (an attacker domain resolving to 127.0.0.1 still sends its own Host);
* there is no CORS middleware at all, so a browser cannot read a cross-origin
  response;
* state-changing routes additionally require a same-origin request, checked from
  ``Origin`` and ``Sec-Fetch-Site``;
* every response carries a restrictive Content-Security-Policy and
  ``X-Content-Type-Options: nosniff``.

None of this needs a secret or a network call.
"""

from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from ..core.errors import ConfigError

#: Host names a browser may use for this machine.
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

#: The vendored chart library injects one small, static stylesheet for its
#: attribution logo, so that exact content is allowed by hash rather than by
#: enabling ``'unsafe-inline'``. Recompute it when upgrading the chart library
#: (see static/vendor/lightweight-charts/README.md).
CHART_STYLE_SHA256 = "sha256-3pRED1tOXas1FXFoPb9TGCjmYe9XQsmO9OV23khV2nY="

#: No CDN, no un-hashed inline style, no framing, no external connections. The
#: chart positions its canvases with style *attributes*, so CSP3's
#: ``style-src-attr`` is the one deliberate relaxation; inline scripts and
#: ``<style>`` blocks stay blocked.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    f"style-src-elem 'self' '{CHART_STYLE_SHA256}'; "
    "style-src-attr 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

LOOPBACK_ONLY_MESSAGE = (
    "The dashboard only listens on this computer. Refusing to bind {host!r} "
    "because it is not a loopback address. Use --host 127.0.0.1 (or localhost)."
)

DNS_REBINDING_MESSAGE = (
    "Refused: this request arrived with the unexpected host name {host!r}. "
    "Open the dashboard at http://127.0.0.1:8765 (or localhost) instead."
)

CROSS_SITE_MESSAGE = (
    "For safety, this action is only accepted from the dashboard's own page. "
    "Open the dashboard at http://127.0.0.1:8765 and try again."
)

CSRF_COOKIE_NAME = "kite_alerts_csrf"
CSRF_HEADER_NAME = "X-Kite-Alerts-CSRF"

CSRF_MISSING_MESSAGE = (
    "This page's session token is missing or stale. Reload the dashboard page and try again."
)
CSRF_MISMATCH_MESSAGE = (
    "This action requires the dashboard's session token. Reload the page and try again."
)


def _valid_csrf_token(value: str | None) -> bool:
    return isinstance(value, str) and len(value) >= 16


def resolve_csrf(request: Request) -> tuple[str, bool]:
    """Return ``(token, issued)``: reuse the session cookie or mint a new token."""
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if _valid_csrf_token(token):
        assert token is not None
        return token, False
    return secrets.token_urlsafe(32), True


def set_csrf_cookie(response: Response, token: str) -> None:
    """Attach the session CSRF cookie (HttpOnly, SameSite=Strict, no expiry)."""
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        path="/",
    )


def require_csrf_token(request: Request) -> None:
    """Raise a plain-language 403 when the CSRF header does not match the cookie."""
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if not _valid_csrf_token(cookie):
        raise HTTPException(status_code=403, detail=CSRF_MISSING_MESSAGE)
    assert cookie is not None
    header = request.headers.get(CSRF_HEADER_NAME.lower())
    if not header or not secrets.compare_digest(header, cookie):
        raise HTTPException(status_code=403, detail=CSRF_MISMATCH_MESSAGE)


def _strip_brackets(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")]
    return value


def is_loopback(host: str) -> bool:
    """True for ``127.0.0.0/8``, ``::1`` and the name ``localhost``."""
    value = _strip_brackets(host)
    if not value:
        return False
    if value in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_bind_host(host: str) -> str:
    """Reject a non-loopback ``--host`` with a message a non-technical user can act on."""
    if not is_loopback(host):
        raise ConfigError(LOOPBACK_ONLY_MESSAGE.format(host=host))
    return host


def host_from_header(host_header: str) -> str:
    """Hostname part of a ``Host`` header, handling a port and IPv6 brackets."""
    value = host_header.strip()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end].lower() if end != -1 else value.lower()
    # A bare IPv6 address (no brackets) is not a valid Host header but be lenient.
    if value.count(":") > 1:
        return value.lower()
    return value.split(":", 1)[0].lower()


def host_header_allowed(host_header: str) -> bool:
    return is_loopback(host_from_header(host_header))


def is_same_origin(request: Request) -> bool:
    """Origin / Sec-Fetch-Site check for state-changing routes.

    A missing ``Origin`` is accepted because non-browser clients (curl, the test
    suite) do not send one; a browser always sends it for a cross-site POST.
    """
    origin = request.headers.get("origin")
    if origin:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https"):
            return False
        if parts.netloc.lower() != (request.headers.get("host") or "").lower():
            return False
    site = (request.headers.get("sec-fetch-site") or "").lower()
    return not site or site in ("same-origin", "none")


def require_same_origin(request: Request) -> None:
    """Raise a plain-language 403 for a cross-site request."""
    if not is_same_origin(request):
        raise HTTPException(status_code=403, detail=CROSS_SITE_MESSAGE)


def _apply_security_headers(response: Response) -> None:
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers.setdefault("Cache-Control", "no-store")


class SecurityMiddleware(BaseHTTPMiddleware):
    """Reject a foreign Host header, then harden every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host_header = request.headers.get("host", "")
        if not host_header_allowed(host_header):
            return JSONResponse(
                status_code=400,
                content={"detail": DNS_REBINDING_MESSAGE.format(host=host_header or "(missing)")},
                headers={
                    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
        response = await call_next(request)
        _apply_security_headers(response)
        return response
