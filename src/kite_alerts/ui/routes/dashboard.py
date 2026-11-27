"""Dashboard shell routes: the HTML page and the vendored-file acknowledgements."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

from ..security import resolve_csrf, set_csrf_cookie
from .shared import LICENSE_FILE, NOTICE_FILE, load_index_html


def dashboard_router() -> APIRouter:
    """Build the router for the dashboard shell (no context needed)."""
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    def index(request: Request) -> Response:
        token, issued = resolve_csrf(request)
        html = load_index_html()
        if "</head>" in html:
            html = html.replace(
                "</head>",
                f'<meta name="kite-alerts-csrf" content="{token}">\n</head>',
                1,
            )
        response = HTMLResponse(html, media_type="text/html; charset=utf-8")
        if issued:
            set_csrf_cookie(response, token)
        return response

    @router.get("/license", include_in_schema=False)
    def license_text() -> FileResponse:
        if not LICENSE_FILE.is_file():
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="The licence file was not bundled.")
        return FileResponse(LICENSE_FILE, media_type="text/plain; charset=utf-8")

    @router.get("/notice", include_in_schema=False)
    def notice_text() -> FileResponse:
        if not NOTICE_FILE.is_file():
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="The notice file was not bundled.")
        return FileResponse(NOTICE_FILE, media_type="text/plain; charset=utf-8")

    return router
