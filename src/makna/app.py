"""Composition root: builds the one FastAPI application that `makna serve` runs.

This is the only place that knows both interfaces. The web routes (WP-16) and the MCP server
(WP-12/WP-13) are mounted here, so `makna.cli` can start the server without importing
`makna.web` or `makna.mcp` itself (ARCHITECTURE section 2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.routing import Route

from makna.config import Settings, load_settings
from makna.mcp.http import PATH as MCP_PATH
from makna.mcp.http import build_mcp_endpoint
from makna.services import build_services
from makna.web import install as install_web


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Without `settings`, load them from the environment.

    Raises `makna.config.ConfigError` when a required variable is missing or the embedding
    provider or dialect cannot be built, so the server fails at start instead of at the first
    request. The services (`app.state.services`) are built here from the settings; building them
    opens no database connection, so the application starts, and `/healthz` answers, while the
    database is still down. The first service call is the first to connect.
    """
    settings = settings or load_settings()
    services = build_services(settings)
    mcp = build_mcp_endpoint(services, settings.mcp.allowed_hosts)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            async with mcp.run():
                yield
        finally:
            services.dispose()

    app = FastAPI(title="Makna", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.services = services

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # The MCP endpoint answers for every method and authenticates each request itself. Both
    # spellings are routed, so a client never meets a redirect.
    for path in (MCP_PATH, MCP_PATH + "/"):
        app.router.routes.append(Route(path, endpoint=mcp.asgi))

    # The web pages: sign-in, sessions, CSRF, account and administration pages (WP-16). Their
    # gate leaves `/mcp`, `/healthz`, and the static files alone.
    install_web(app, settings, services)

    return app
