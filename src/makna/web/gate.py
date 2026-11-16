"""The gate in front of every web page: who is signed in, and the CSRF check.

This is an ASGI middleware that works **by default deny**. A route added later is protected
without anyone remembering to protect it, because the gate decides on the path before the router
sees the request:

- `/mcp` (and `/mcp/...`) is passed on untouched. The gate never reads a cookie there, so a
  session cookie authenticates nothing under `/mcp`; the endpoint has its own bearer-token check.
- `/healthz` and `/static/...` are public.
- `/login` is public. It is protected against cross-site posts by the login cookie, and it does
  not read the session cookie: signing in always issues a new session.
- Every other path needs a session. On every request the gate takes the user id from the signed
  cookie and asks the user store for the `Caller` (`caller_for_user`, in a worker thread), so a
  user who was deactivated or whose role changed is affected on the very next request. An
  unknown or inactive user gets the cookie cleared and a redirect to `/login`; a user store that
  cannot answer gets a 503 page.
- Every request with a method other than GET, HEAD, or OPTIONS needs a CSRF token equal to the
  CSRF secret of its session (of the login cookie, for `/login`). The token is the header
  `X-CSRF-Token` (HTMX) or the form field `csrf_token` of an urlencoded body.

Every response of the web pages carries the headers of `SECURITY_HEADERS`, and everything but
static files is `Cache-Control: no-store`.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qsl, quote

import anyio.to_thread
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from makna.services import Services
from makna.services.accounts import AccountStoreError, AuthenticationError
from makna.web.pages import CSRF_FAILED, SCOPE_KEY, Pages, WebContext
from makna.web.security import (
    CookieCodec,
    CookieNames,
    safe_next,
    tokens_match,
)

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
LOGIN_PATH = "/login"
CHANNEL = "web"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HSTS = "max-age=31536000"
MAX_BODY = 1024 * 1024
"""Bytes of a request body the gate reads to find the CSRF field (a form, not an upload)."""

# One place for the policy. The editor (WP-19) will have to allow what Ace needs here.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
    (
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'",
    ),
)


def session_cookie_options(development: bool) -> dict[str, Any]:
    """The attributes of every cookie of the web pages: `HttpOnly`, `SameSite=Lax`, `Secure`
    unless this is the development stack (plain http), no `Domain` (so host-only)."""
    return {"path": "/", "secure": not development, "httponly": True, "samesite": "lax"}


def is_mcp(path: str) -> bool:
    return path == MCP_PATH or path.startswith(MCP_PATH + "/")


def is_static(path: str) -> bool:
    return path.startswith("/static/")


class WebGate:
    """See the module text. `services` is used for its account store only."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        services: Services,
        codec: CookieCodec,
        pages: Pages,
        development: bool,
    ) -> None:
        self._app = app
        self._accounts = services.accounts
        self._codec = codec
        self._pages = pages
        self._development = development
        self._names = CookieNames.for_mode(development)

    # --- cookies ---

    def clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(self._names.session, **session_cookie_options(self._development))

    # --- the request ---

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self._app(scope, receive, send)
            return
        path: str = scope["path"]
        if is_mcp(path):
            await self._app(scope, receive, send)
            return
        static = is_static(path)
        send = self._with_headers(send, cache=not static)
        if static or path == "/healthz":
            await self._run(scope, receive, send)
            return
        try:
            admitted = await self._admit(scope, receive, send, path)
        except Exception as error:
            # Never a page with text, and never a pass: only the class is logged. The page that
            # was asked for has not run yet, so nothing has been sent.
            logger.error("web: the gate failed unexpectedly: error=%s", type(error).__name__)
            await self._pages.error(scope, 503)(scope, receive, send)
            return
        if admitted is not None:
            await self._run(admitted[0], admitted[1], send)

    async def _run(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the page. A crash is answered here with the fixed 500 page and logged by its class
        only: it is not raised on to the server, which would log the traceback and the message
        (they can hold a value of the request)."""
        started = False

        async def tracking(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self._app(scope, receive, tracking)
        except Exception as error:
            logger.error("web: unhandled error: error=%s", type(error).__name__)
            if not started:
                await self._pages.error(scope, 500)(scope, receive, send)

    def _with_headers(self, send: Send, *, cache: bool) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers.setdefault(name, value)
                if not self._development:
                    # The pages are meant for https only (the cookies are `Secure`).
                    headers.setdefault("Strict-Transport-Security", HSTS)
                if cache:
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        return wrapped

    async def _admit(
        self, scope: Scope, receive: Receive, send: Send, path: str
    ) -> tuple[Scope, Receive] | None:
        """The scope and `receive` to run the page with, or None after answering the request
        here (sign in first, a 503, a failed CSRF check)."""
        request = Request(scope)
        method: str = scope["method"]
        web: WebContext | None = None
        clear = False
        if path != LOGIN_PATH:
            raw = request.cookies.get(self._names.session)
            session = self._codec.load_session(raw)
            clear = bool(raw) and session is None
            if session is not None:
                try:
                    caller = await anyio.to_thread.run_sync(
                        self._accounts.caller_for_user, session.user_id, CHANNEL
                    )
                except AuthenticationError:
                    session, clear = None, True
                except AccountStoreError:
                    await self._pages.error(scope, 503)(scope, receive, send)
                    return None
                else:
                    web = WebContext(session, caller)
            if web is None:
                await self._sign_in_first(request, clear)(scope, receive, send)
                return None
            scope = {**scope, SCOPE_KEY: web}
        if method not in SAFE_METHODS:
            secret = (
                web.session.csrf
                if web
                else self._codec.load_login(request.cookies.get(self._names.login))
            )
            outcome = await self._check_csrf(scope, receive, secret)
            if isinstance(outcome, Response):
                await outcome(scope, receive, send)
                return None
            if outcome is None:
                return None  # the client went away
            receive = outcome
        return scope, receive

    def _sign_in_first(self, request: Request, clear: bool) -> Response:
        """The answer to a request that needs a session and has none."""
        target = LOGIN_PATH
        if request.method in ("GET", "HEAD"):
            wanted = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            if safe_next(wanted) != "/":
                target = f"{LOGIN_PATH}?next={quote(wanted, safe='')}"
        if request.headers.get("hx-request") == "true":
            # A redirect would be followed by the browser and the login page swapped into the
            # fragment; HTMX is told to navigate instead.
            response: Response = Response(status_code=401, headers={"HX-Redirect": LOGIN_PATH})
        else:
            response = RedirectResponse(target, status_code=303)
        if clear:
            self.clear_session_cookie(response)
        return response

    async def _check_csrf(
        self, scope: Scope, receive: Receive, secret: str | None
    ) -> Response | Receive | None:
        """None when the client went away; a `Response` to send instead of the page; else the
        `receive` the page must read its body from (the body may have been read already)."""
        request = Request(scope)
        if not secret:
            return self._pages.error(scope, 403, CSRF_FAILED)
        header = request.headers.get("x-csrf-token")
        if header is not None:
            if tokens_match(header, secret):
                return receive
            return self._pages.error(scope, 403, CSRF_FAILED)
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
            return self._pages.error(scope, 413)
        body = await _read_body(receive)
        if body is None:
            return None
        if len(body) > MAX_BODY:
            return self._pages.error(scope, 413)
        given = _form_token(request.headers.get("content-type", ""), body)
        if given is None or not tokens_match(given, secret):
            return self._pages.error(scope, 403, CSRF_FAILED)
        return _replay(body, receive)


async def _read_body(receive: Receive) -> bytes | None:
    """The whole request body, or None when the client disconnected. Stops reading a little past
    `MAX_BODY`, so a body of any size costs bounded memory."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        chunks.append(chunk)
        if size > MAX_BODY or not message.get("more_body", False):
            return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    """A `receive` that gives the page the body already read, then whatever comes next."""
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


def _form_token(content_type: str, body: bytes) -> str | None:
    """The one `csrf_token` field of an urlencoded body, or None (no such field, more than one,
    another content type, or a body that is not text)."""
    if content_type.split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        return None
    try:
        fields = parse_qsl(
            body.decode("utf-8"), keep_blank_values=True, strict_parsing=False, max_num_fields=500
        )
    except (UnicodeDecodeError, ValueError):
        return None
    found = [value for name, value in fields if name == "csrf_token"]
    return found[0] if len(found) == 1 else None
