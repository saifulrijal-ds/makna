"""Web app: routes, templates, and static files (ARCHITECTURE section 13).

`install(app, settings, services)` adds the pages to the application: the routes, the static
files, the error pages, and the gate that signs users in and checks CSRF tokens on every request
(`makna.web.gate`). Only `makna.app` calls it. The package imports `makna.services` and
`makna.model` and holds form handling, not logic.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse, Response
from starlette.staticfiles import StaticFiles

from makna.config import Settings
from makna.services import Services
from makna.services.accounts import AccountStoreError
from makna.web.bench import bench_router
from makna.web.discovery import router as discovery
from makna.web.editor import editor_router
from makna.web.gate import SECURITY_HEADERS, WebGate, is_mcp
from makna.web.pages import Pages
from makna.web.routes import WebState, admin, router
from makna.web.security import CookieCodec, LoginThrottle

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

__all__ = ["install"]


def install(app: FastAPI, settings: Settings, services: Services) -> None:
    """Add the web pages to `app`. The session cookies are signed with a key derived from
    `settings.secret_key`; `settings.web.development` leaves the `Secure` flag off them."""
    pages = Pages()
    codec = CookieCodec(settings.secret_key.get_secret_value())
    app.state.web = WebState(
        services=services,
        codec=codec,
        throttle=LoginThrottle(),
        pages=pages,
        development=settings.web.development,
    )
    app.include_router(router)
    app.include_router(admin)
    app.include_router(bench_router)
    app.include_router(editor_router)
    app.include_router(discovery)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def answer(request: Request, status: int, kind: int | str | None = None) -> Response:
        return pages.error(request.scope, status, kind)

    async def http_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, HTTPException)
        response = answer(request, error.status_code)
        if error.headers:  # 405 carries the `Allow` header, which holds no request data
            response.headers.update(error.headers)
        return response

    async def invalid_request(request: Request, error: Exception) -> Response:
        # A malformed id in the path is a page that does not exist; anything else is a bad form.
        assert isinstance(error, RequestValidationError)
        in_path = any(item.get("loc", ("",))[0] == "path" for item in error.errors())
        return answer(request, 404 if in_path else 400)

    async def store_down(request: Request, error: Exception) -> Response:
        logger.error("web: the user store is not available")
        return answer(request, 503)

    async def crashed(request: Request, error: Exception) -> Response:
        # Outside the gate: the page is fixed text, and only the class goes to the log.
        logger.error("web: unhandled error: error=%s", type(error).__name__)
        if is_mcp(request.url.path):
            return PlainTextResponse("Internal Server Error", status_code=500)
        response = answer(request, 500)
        for name, value in SECURITY_HEADERS:
            response.headers.setdefault(name, value)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(AccountStoreError, store_down)
    app.add_exception_handler(Exception, crashed)
    app.add_middleware(
        WebGate,
        services=services,
        codec=codec,
        pages=pages,
        development=settings.web.development,
    )
