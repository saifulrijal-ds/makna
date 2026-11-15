"""Streamable HTTP for the MCP server: bearer-token authentication on every request.

`build_mcp_endpoint(services)` gives the ASGI endpoint that `makna.app` mounts at `/mcp` and the
session manager the application's lifespan must run. What it does, in order, for every request
(POST, GET, DELETE, anything else) before the SDK sees it:

1. Read the one `Authorization` header and take the token of `Bearer <token>` (the scheme in any
   letter case, whitespace around the token ignored).
2. Check the token with `Services.authenticate_token` on the `mcp` channel, off the event loop.
   That gives the `Caller`: the token's user, with `token_id` set, so at most viewer permissions.
   A token revoked since the last call is refused on this one, because nothing is cached.
3. Pass the request on with the `Caller` in the ASGI scope of that request only, and without
   the `Authorization` header, so the token never reaches the SDK or a tool.

A missing header, another scheme, an empty value, a malformed, unknown, or revoked token, and a
token of an inactive user all get the same `401`: same status, headers, and body. A user store
that cannot answer is `503` with a fixed body. Neither response says anything else, and the
header is never logged.

The server is stateless (`stateless_http`): every request stands alone, with no session id to
steal or replay, and the caller is the one this request authenticated. The tools need no state.
Host and Origin checking of the SDK (DNS rebinding protection) stays on; see `transport_security`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import Context
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from makna.mcp.server import build_mcp_server
from makna.services import Caller, Services, authenticate_bearer_token
from makna.services.accounts import AccountStoreError, AuthenticationError

logger = logging.getLogger(__name__)

CALLER_KEY = "makna.caller"
"""The ASGI scope key under which the middleware stores the `Caller` of one request."""
CHANNEL = "mcp"
PATH = "/mcp"

_JSON = "application/json"
UNAUTHORIZED_BODY = (
    b'{"error":{"code":"UNAUTHENTICATED","message":"A valid API token is required. '
    b'Send it as the header Authorization: Bearer <token>."}}'
)
UNAVAILABLE_BODY = (
    b'{"error":{"code":"UNAVAILABLE","message":"Makna cannot check the token right now. '
    b'Try again later."}}'
)
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
"""Hosts the server answers to without configuration (the SDK's own localhost defaults)."""


def parse_bearer(value: str | None) -> str | None:
    """The token of an `Authorization` header value, or `None` when it is not `Bearer <token>`.

    The scheme is matched in any letter case. The token is whatever follows the scheme and white
    space, stripped; whether it is a token at all is for `authenticate_token` to say.
    """
    if value is None:
        return None
    parts = value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _unauthorized() -> Response:
    return Response(
        UNAUTHORIZED_BODY, status_code=401, media_type=_JSON, headers={"WWW-Authenticate": "Bearer"}
    )


def _unavailable() -> Response:
    return Response(UNAVAILABLE_BODY, status_code=503, media_type=_JSON)


class BearerTokenAuth:
    """ASGI middleware: authenticate every HTTP request, then hand it on with its `Caller`.

    `authenticate` takes the token and returns the `Caller`, or raises `AuthenticationError`
    (a 401) or `AccountStoreError` (a 503). It is synchronous and runs in a worker thread.
    """

    def __init__(self, app: ASGIApp, authenticate: Callable[[str], Caller]) -> None:
        self._app = app
        self._authenticate = authenticate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        headers: list[tuple[bytes, bytes]] = list(scope.get("headers", ()))
        given = [value for name, value in headers if name.lower() == b"authorization"]
        # Two Authorization headers are ambiguous, and an ambiguous request is not authenticated.
        token = parse_bearer(given[0].decode("latin-1")) if len(given) == 1 else None
        if token is None:
            await _unauthorized()(scope, receive, send)
            return
        try:
            caller = await anyio.to_thread.run_sync(self._authenticate, token)
        except AuthenticationError:
            await _unauthorized()(scope, receive, send)
            return
        except AccountStoreError:
            logger.error("mcp: the user store is not available")
            await _unavailable()(scope, receive, send)
            return
        except Exception as error:
            # Never a 500 with text, and never a pass: only the class is logged.
            logger.error("mcp: authentication failed unexpectedly: error=%s", type(error).__name__)
            await _unavailable()(scope, receive, send)
            return
        kept = [(name, value) for name, value in headers if name.lower() != b"authorization"]
        await self._app({**scope, "headers": kept, CALLER_KEY: caller}, receive, send)


class NoCallerError(Exception):
    """A tool call arrived without an authenticated caller. It is `INTERNAL` to the agent."""


def caller_from_request(context: Context[Any, Any] | None) -> Caller:
    """The `Caller` the middleware stored in the ASGI scope of the request of this call."""
    if context is None:
        raise NoCallerError
    try:
        request = context.request_context.request
        if request is None:
            raise NoCallerError
        caller = request.scope[CALLER_KEY]
    except (AttributeError, KeyError, ValueError, TypeError):
        raise NoCallerError from None
    if not isinstance(caller, Caller):
        raise NoCallerError
    return caller


def transport_security(allowed_hosts: Sequence[str] = ()) -> TransportSecuritySettings:
    """The SDK's Host and Origin check, for localhost and the hosts named in the configuration.

    A request whose `Host` is not on the list gets `421`, and one whose `Origin` is not gets
    `403`; a request without an `Origin` (an agent, not a web page) passes the second check.
    `allowed_hosts` are bare host names, or `host:port` (each also matches with any port when
    given without one).
    """
    hosts = [f"{host}:*" for host in LOCAL_HOSTS]
    origins = [f"http://{host}:*" for host in LOCAL_HOSTS]
    for host in allowed_hosts:
        hosts.append(host)
        origins += [f"http://{host}", f"https://{host}"]
        if ":" not in host.rpartition("]")[2]:
            hosts.append(f"{host}:*")
            origins += [f"http://{host}:*", f"https://{host}:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


@dataclass(frozen=True)
class McpEndpoint:
    """What `makna.app` mounts: the endpoint, and the manager the lifespan must run."""

    asgi: ASGIApp
    manager: StreamableHTTPSessionManager

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Run the SDK's session manager: the application's lifespan enters this once."""
        async with self.manager.run():
            yield


def build_mcp_endpoint(services: Services, allowed_hosts: Sequence[str] = ()) -> McpEndpoint:
    """The authenticated, stateless Streamable HTTP endpoint of the MCP server for `services`.

    Building it opens no database connection. The manager can run once: build a new endpoint
    for a new application.
    """
    server = build_mcp_server(services, caller_from_request)
    # The SDK makes its session manager here. Its Starlette app is not used: that app serves
    # its path only with a trailing-slash redirect when mounted, and a redirect is not "/mcp".
    server.streamable_http_app(
        streamable_http_path=PATH,
        json_response=True,
        stateless_http=True,
        transport_security=transport_security(allowed_hosts),
    )
    manager = server.session_manager
    asgi = BearerTokenAuth(
        StreamableHTTPASGIApp(manager),
        lambda raw: authenticate_bearer_token(services, raw, CHANNEL),
    )
    return McpEndpoint(asgi, manager)
