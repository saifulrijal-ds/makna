"""Shared pieces of the WP-13 integration tests: the application with its MCP endpoint over the
service world of `service_support`, API tokens for its users, the SDK's client in process and over
HTTP, and a real server on a free port (uvicorn in a thread)."""

from __future__ import annotations

import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

import httpx2
import uvicorn
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, Tool
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from makna.app import create_app
from makna.config import Settings
from makna.mcp.server import build_mcp_server
from makna.services import accounts

from .service_support import Users, services

HOST = "testserver"
MCP_URL = f"http://{HOST}/mcp"
ROLES = ("viewer", "curator", "approver", "admin")


@dataclass(frozen=True)
class Tokens:
    """A token for each user of the service world, by role: `{role}@example.com`."""

    by_role: dict[str, str]
    ids: dict[str, int]


def make_tokens(engine: Engine) -> Tokens:
    tokens: dict[str, str] = {}
    ids: dict[str, int] = {}
    for role in ROLES:
        new = accounts.create_token(engine, f"{role}@example.com", f"mcp test {role}")
        tokens[role] = new.token
        ids[role] = new.id
    return Tokens(tokens, ids)


def make_settings(url: URL, allowed_hosts: tuple[str, ...] = (HOST,)) -> Settings:
    return Settings.model_validate(
        {
            "database_url": url.render_as_string(hide_password=False),
            "secret_key": secrets.token_hex(32),
            "embedding_provider": "hash",
            "mcp": {"allowed_hosts": list(allowed_hosts)},
        }
    )


def make_app(engine: Engine, allowed_hosts: tuple[str, ...] = (HOST,)) -> FastAPI:
    """The real application (`create_app`) on the database of `engine`."""
    return create_app(make_settings(engine.url, allowed_hosts))


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[FastAPI]:
    """Run the application's lifespan (it runs the MCP session manager), as a server would."""
    async with app.router.lifespan_context(app):
        yield app


def http_client(app: FastAPI, token: str | None = None, **headers: str) -> httpx2.AsyncClient:
    """An HTTP client that calls the application in process, with a bearer token if given."""
    sent = dict(headers)
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=f"http://{HOST}", headers=sent
    )


@asynccontextmanager
async def mcp_client(app: FastAPI, token: str) -> AsyncIterator[Client]:
    """The SDK's client, over the application's HTTP endpoint, authenticated with `token`."""
    async with http_client(app, token) as http:
        async with Client(streamable_http_client(MCP_URL, http_client=http)) as client:
            yield client


@asynccontextmanager
async def in_process_client(
    engine: Engine, users: Users, role: str = "viewer"
) -> AsyncIterator[Client]:
    """The SDK's client talking to the same server in process (no HTTP), as a token's caller."""
    caller = users.token(role)
    server = build_mcp_server(services(engine), lambda _context: caller)
    async with Client(server) as client:
        yield client


def text_of(result: CallToolResult) -> str:
    assert len(result.content) == 1, result.content
    block = result.content[0]
    assert block.type == "text"
    return str(block.text)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Sink(logging.Handler):
    """Collects every log line of the loggers it is attached to."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage() + (record.exc_text or ""))


LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi", "mcp", "httpx2")


@contextmanager
def real_server(app: FastAPI, sink: Sink | None = None) -> Iterator[str]:
    """The application served by uvicorn on a free port, in a thread. Yields its base URL.

    With `sink`, uvicorn runs at its debug level and every line of its loggers, of the SDK's, and
    of the root logger is collected in it.
    """
    port = free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="debug" if sink is not None else "warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    watched = [logging.getLogger(name) for name in LOGGERS] if sink is not None else []
    for logger in watched:
        logger.addHandler(sink)  # type: ignore[arg-type]
    old_level = logging.getLogger().level
    if sink is not None:
        logging.getLogger().setLevel(logging.DEBUG)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the test server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        for logger in watched:
            logger.removeHandler(sink)  # type: ignore[arg-type]
        logging.getLogger().setLevel(old_level)


SNAPSHOT_HEADER = (
    "# MCP server snapshot (reviewed text). Changing a word here is a reviewed change (MCP-2)."
)


def render_snapshot(instructions: str, tools: list[Tool]) -> str:
    """The text the reviewed snapshot file holds: the instructions, then for each tool its
    description, annotations, and input schema as a client receives them."""
    lines = [SNAPSHOT_HEADER, "", "## instructions", instructions, ""]
    for tool in tools:
        assert tool.annotations is not None
        annotations = tool.annotations.model_dump(by_alias=True, exclude_none=True)
        lines += [
            f"## {tool.name}",
            "description:",
            str(tool.description),
            "annotations: " + json.dumps(annotations, sort_keys=True),
            "inputSchema:",
            json.dumps(tool.input_schema, indent=2, sort_keys=True),
            "",
        ]
    return "\n".join(lines)
