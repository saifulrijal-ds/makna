"""`makna mcp`: the stdio-to-HTTP bridge for MCP clients that can only start a local process.

It holds nothing but `MAKNA_URL` and `MAKNA_TOKEN` (ARCHITECTURE section 12). It forwards every
MCP message, unread and unchanged, between its standard input and output and the Makna server's
`/mcp` endpoint, with `Authorization: Bearer <token>` on every request. The tool list, the
instructions, and every answer therefore come from the server; the bridge declares none.

This module must stay free of everything the server side is made of: no `makna.services`, no
`makna.config` settings, no database or search library, no other Makna module. A test loads it
in a fresh interpreter and checks that none of them is imported. It reads the two variables
above and no other setting, so an agent's machine never holds the database URL.

- `MAKNA_URL` is the base URL of the application, for example `http://localhost:8000`; the bridge
  appends `/mcp` (a URL that already ends in `/mcp` is taken as the base it names, so it is not
  doubled). `http://` is accepted only for localhost, because the token travels in every request,
  and a URL with a user name or password is refused.
- A missing variable is one line on standard error naming the variable (never a value) and exit
  status 2, before any connection is opened.
- A `401` from the server ends the bridge with a fixed line on standard error and status 1. The
  token and the server's answer are never printed.
- Standard output carries MCP messages only: the SDK's stdio transport diverts anything else a
  library prints to standard error.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp_types import JSONRPCError, JSONRPCRequest, JSONRPCResponse

URL_VARIABLE = "MAKNA_URL"
TOKEN_VARIABLE = "MAKNA_TOKEN"
ENDPOINT_PATH = "/mcp"
REFUSED_MESSAGE = "makna mcp: the Makna server refused the token"
UNREACHABLE_MESSAGE = "makna mcp: cannot reach the Makna server"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
EXIT_REFUSED = 1
EXIT_BAD_SETTINGS = 2
DRAIN_SECONDS = 30.0
"""How long the bridge waits, after its input has closed, for answers still on their way."""

_TOKEN_CHARACTERS = re.compile(r"[\x21-\x7e]+")


class BridgeSettingsError(Exception):
    """A variable is missing or unusable. The message names the variable, never a value."""


def settings_from(environ: Mapping[str, str]) -> tuple[str, str]:
    """The endpoint URL and the token, from `environ`; raises `BridgeSettingsError`.

    Reads `MAKNA_URL` and `MAKNA_TOKEN` and nothing else. A value that is empty after
    stripping is missing.
    """
    url = environ.get(URL_VARIABLE, "").strip()
    token = environ.get(TOKEN_VARIABLE, "").strip()
    missing = [name for name, value in ((URL_VARIABLE, url), (TOKEN_VARIABLE, token)) if not value]
    if missing:
        raise BridgeSettingsError(f"{' and '.join(missing)} not set")
    if not url.lower().startswith(("http://", "https://")):
        raise BridgeSettingsError(f"{URL_VARIABLE} must start with http:// or https://")
    try:
        parts = urlsplit(url)
        host, user, password = parts.hostname, parts.username, parts.password
    except ValueError:
        raise BridgeSettingsError(f"{URL_VARIABLE} is not a URL") from None
    if not host:
        raise BridgeSettingsError(f"{URL_VARIABLE} is not a URL")
    if user is not None or password is not None:
        raise BridgeSettingsError(f"{URL_VARIABLE} must not hold a user name or password")
    if parts.scheme.lower() == "http" and host.lower() not in LOCAL_HOSTS:
        raise BridgeSettingsError(
            f"{URL_VARIABLE} must use https:// (http:// is for localhost only), "
            "because the token is sent with every request"
        )
    if _TOKEN_CHARACTERS.fullmatch(token) is None:
        raise BridgeSettingsError(f"{TOKEN_VARIABLE} holds a character a token never has")
    base = url.rstrip("/")
    if base.endswith(ENDPOINT_PATH):
        base = base[: -len(ENDPOINT_PATH)].rstrip("/")
    return base + ENDPOINT_PATH, token


class _ClassOnlyHandler(logging.Handler):
    """What the SDK's and the HTTP client's loggers print on standard error: the level, the
    logger, and the class of the exception, never a message, its arguments, or a traceback. A
    message can hold what the server sent (a malformed answer is quoted by the SDK's parser)."""

    def emit(self, record: logging.LogRecord) -> None:
        error = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else None
        suffix = f": {error}" if error else ""
        print(f"makna mcp: {record.levelname} {record.name}{suffix}", file=sys.stderr)


LIBRARY_LOGGERS = ("mcp", "httpx2", "httpcore2")


def quiet_library_logs() -> None:
    """Give the libraries' loggers the handler above and stop them reaching any other."""
    for name in LIBRARY_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers = [_ClassOnlyHandler(logging.WARNING)]
        logger.setLevel(logging.WARNING)
        logger.propagate = False


class _Unreachable(httpx2.AsyncHTTPTransport):
    """The HTTP transport, telling the bridge when the server cannot be connected to (refused,
    no such host, TLS), which the SDK would otherwise leave waiting for an answer."""

    def __init__(self, on_unreachable: Callable[[], None]) -> None:
        super().__init__()
        self._on_unreachable = on_unreachable

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            return await super().handle_async_request(request)
        except (httpx2.ConnectError, httpx2.ConnectTimeout):
            self._on_unreachable()
            raise


class _Bridge:
    def __init__(self) -> None:
        self.refused = False
        self.unreachable = False
        self.pending: set[str | int] = set()
        self.idle = anyio.Event()
        self.idle.set()

    def sent(self, message: SessionMessage) -> None:
        body = message.message
        if isinstance(body, JSONRPCRequest):
            self.pending.add(body.id)
            self.idle = anyio.Event()

    def answered(self, message: SessionMessage) -> None:
        body = message.message
        if isinstance(body, (JSONRPCResponse, JSONRPCError)) and body.id in self.pending:
            self.pending.discard(body.id)
            if not self.pending:
                self.idle.set()


async def _bridge(url: str, token: str) -> int:
    state = _Bridge()

    async def check_response(response: httpx2.Response) -> None:
        if response.status_code == 401:
            state.refused = True
            scope.cancel()

    def unreachable() -> None:
        state.unreachable = True
        scope.cancel()

    client = httpx2.AsyncClient(
        transport=_Unreachable(unreachable),
        headers={"Authorization": f"Bearer {token}"},
        event_hooks={"response": [check_response]},
        timeout=httpx2.Timeout(30.0, read=300.0),
        follow_redirects=False,
    )
    try:
        with anyio.CancelScope() as scope:
            async with (
                client,
                streamable_http_client(url, http_client=client) as (http_read, http_write),
                stdio_server() as (stdin_read, stdout_write),
                anyio.create_task_group() as tasks,
            ):

                async def to_server() -> None:
                    async for item in stdin_read:
                        if isinstance(item, Exception):
                            # A line that is not a JSON-RPC message. Nothing of it is repeated.
                            print(
                                "makna mcp: skipped input that is not an MCP message",
                                file=sys.stderr,
                            )
                            continue
                        state.sent(item)
                        await http_write.send(item)
                    # Input closed: let the answers still on their way arrive, then stop.
                    with anyio.move_on_after(DRAIN_SECONDS):
                        await state.idle.wait()
                    # Everything that arrived is handed to the writer: close the output so that
                    # it writes what it holds and ends, and stop only the forwarding tasks.
                    await stdout_write.aclose()
                    tasks.cancel_scope.cancel()

                async def to_client() -> None:
                    async for item in http_read:
                        if isinstance(item, Exception):
                            print("makna mcp: a message from the server was lost", file=sys.stderr)
                            continue
                        await stdout_write.send(item)
                        state.answered(item)

                tasks.start_soon(to_server)
                tasks.start_soon(to_client)
    except Exception:
        if not (state.unreachable or state.refused):
            raise  # the failure that was announced is reported below, in its own words
    if state.refused:
        print(REFUSED_MESSAGE, file=sys.stderr)
        return EXIT_REFUSED
    if state.unreachable:
        print(UNREACHABLE_MESSAGE, file=sys.stderr)
        return EXIT_REFUSED
    return 0


def _leaf(error: BaseException) -> BaseException:
    """The first error inside any (nested) exception group: what actually went wrong."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def main(environ: Mapping[str, str] | None = None) -> int:
    """Run the bridge until its input closes. Returns the exit status."""
    try:
        url, token = settings_from(os.environ if environ is None else environ)
    except BridgeSettingsError as error:
        print(f"makna mcp: {error}", file=sys.stderr)
        return EXIT_BAD_SETTINGS
    quiet_library_logs()
    try:
        return anyio.run(_bridge, url, token)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        # The text of an error can carry the URL or a header; report the class only.
        print(f"makna mcp: stopped by {type(_leaf(error)).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
