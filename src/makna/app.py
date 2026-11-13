"""Composition root: builds the one FastAPI application that `makna serve` runs.

This is the only place that knows both interfaces. The web routes (WP-16) and the MCP server
(WP-12/WP-13) are mounted here, so `makna.cli` can start the server without importing
`makna.web` or `makna.mcp` itself (ARCHITECTURE section 2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from makna.config import Settings, load_settings
from makna.services import build_services


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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            services.dispose()

    app = FastAPI(title="Makna", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.services = services

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
