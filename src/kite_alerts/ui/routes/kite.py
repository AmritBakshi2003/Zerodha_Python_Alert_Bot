"""Guided Kite Connect login/logout routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..kite_connect import STATE_COOKIE, ConnectError
from ..security import require_csrf_token, require_same_origin
from ..settings_schema import connect_payload
from .shared import ManualToken, _error

if TYPE_CHECKING:
    from ..app import UiContext


def kite_router(context: UiContext) -> APIRouter:
    """Build the router for the guided Kite Connect surface."""
    router = APIRouter()

    @router.post("/api/kite/login")
    def kite_login(request: Request) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        assert context.kite is not None
        try:
            start = context.kite.start()
        except ConnectError as exc:
            return _error(400, exc.message, exc.code)
        response = JSONResponse(
            {
                "ok": True,
                "start_url": start.start_url,
                "redirect_url": start.redirect_url,
                "expires_in": start.expires_in,
                "message": (
                    "Opening Zerodha's login page. Log in there — this app never sees your "
                    "password or one-time code."
                ),
            }
        )
        response.set_cookie(
            STATE_COOKIE,
            start.state,
            max_age=start.expires_in,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @router.get("/kite/start", include_in_schema=False)
    def kite_start() -> RedirectResponse:
        assert context.kite is not None
        target = context.kite.pending_login_url()
        if target is None:
            return RedirectResponse("/?connect=no_attempt#/connect", status_code=303)
        return RedirectResponse(target, status_code=302)

    def _failed_callback(code: str) -> RedirectResponse:
        return RedirectResponse(f"/?connect={code}#/connect", status_code=303)

    @router.get("/callback", include_in_schema=False)
    def callback(
        request: Request,
        request_token: str = "",
        state: str = "",
        status: str = "",
    ) -> RedirectResponse:
        assert context.kite is not None
        if status and status != "success":
            context.kite.cancel_pending()
            return _failed_callback("cancelled")
        effective_state = state or request.cookies.get(STATE_COOKIE, "")
        try:
            context.kite.finish(request_token, effective_state)
        except ConnectError as exc:
            return _failed_callback(exc.code)
        response = RedirectResponse("/?connect=connected#/connect", status_code=303)
        response.delete_cookie(STATE_COOKIE, path="/")
        return response

    @router.post("/api/kite/manual-token")
    def manual_token(request: Request, payload: ManualToken) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        assert context.kite is not None
        effective_state = payload.state or request.cookies.get(STATE_COOKIE, "")
        try:
            result = context.kite.finish(payload.request_token, effective_state)
        except ConnectError as exc:
            return _error(400, exc.message, exc.code)
        return JSONResponse(result.as_dict())

    @router.get("/api/kite/session")
    def kite_session() -> dict[str, Any]:
        assert context.kite is not None
        session = context.kite.session()
        payload = session.as_dict()
        payload["redirect_url"] = context.kite.redirect_url
        payload["token_present"] = bool(context.settings and context.settings.kite.token)
        payload["credentials"] = connect_payload(
            context.settings, settings_error=context.settings_error
        )
        return payload

    @router.post("/api/kite/disconnect")
    def kite_disconnect(request: Request) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        assert context.kite is not None
        return JSONResponse(context.kite.disconnect().as_dict())

    @router.get("/api/data-sources")
    def data_sources(refresh: bool = False) -> dict[str, Any]:
        assert context.kite is not None
        return context.kite.data_sources(force=refresh)

    # ------------------------------------------------------- simulator controls
    @router.post("/api/kite/simulate/disconnect")
    def sim_disconnect(request: Request) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        if not context.simulate_kite or context.simulator is None:
            raise HTTPException(status_code=400, detail="Simulator mode is not active.")
        context.simulator.trigger_disconnect()
        return JSONResponse({"ok": True, "action": "disconnect"})

    @router.post("/api/kite/simulate/token-expiry")
    def sim_token_expiry(request: Request) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        if not context.simulate_kite or context.simulator is None:
            raise HTTPException(status_code=400, detail="Simulator mode is not active.")
        context.simulator.trigger_token_expiry()
        return JSONResponse({"ok": True, "action": "token_expiry"})

    @router.post("/api/kite/simulate/reconnect")
    def sim_reconnect(request: Request) -> JSONResponse:
        require_same_origin(request)
        require_csrf_token(request)
        if not context.simulate_kite or context.simulator is None:
            raise HTTPException(status_code=400, detail="Simulator mode is not active.")
        context.simulator.trigger_reconnect()
        return JSONResponse({"ok": True, "action": "reconnect"})

    return router
