"""WP-13 unit tier: the bearer-token middleware and the `[mcp]` setting, without a database.

The middleware is driven through httpx's ASGI transport with a fake `authenticate`, in front of a
downstream app that reports what it received.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, MutableMapping
from typing import Any

import anyio
import httpx2
import pytest
from starlette.types import Receive, Scope, Send

from makna.config import ConfigError, load_settings
from makna.mcp.http import (
    CALLER_KEY,
    UNAUTHORIZED_BODY,
    UNAVAILABLE_BODY,
    BearerTokenAuth,
    parse_bearer,
    transport_security,
)
from makna.services import Caller
from makna.services.accounts import AccountStoreError, AuthenticationError

TOKEN = "mkn_" + "A" * 43
CALLER = Caller(user_id=3, role="admin", channel="mcp", token_id=9)


@pytest.mark.parametrize(
    ("header", "token"),
    [
        (f"Bearer {TOKEN}", TOKEN),
        (f"bearer {TOKEN}", TOKEN),
        (f"BEARER {TOKEN}", TOKEN),
        (f"  Bearer   {TOKEN}  ", TOKEN),
        (f"Bearer\t{TOKEN}", TOKEN),
        ("Bearer a b", "a b"),  # whether it is a token is for the account store to say
        (None, None),
        ("", None),
        ("   ", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("Bearer    ", None),
        (TOKEN, None),
        (f"Basic {TOKEN}", None),
        (f"Token {TOKEN}", None),
        (f"Bearerx {TOKEN}", None),
        (f"xBearer {TOKEN}", None),
    ],
)
def test_the_token_is_what_follows_the_bearer_scheme(header: str | None, token: str | None) -> None:
    assert parse_bearer(header) == token


class Downstream:
    """The app behind the middleware: it keeps the scope it was given."""

    def __init__(self) -> None:
        self.scopes: list[Scope] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.scopes.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def drive(
    authenticate: Callable[[str], Caller],
    headers: Any = None,
    method: str = "POST",
) -> tuple[httpx2.Response, Downstream]:
    downstream = Downstream()

    async def go() -> httpx2.Response:
        app = BearerTokenAuth(downstream, authenticate)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://t") as http:
            return await http.request(method, "/mcp", headers=headers)

    return anyio.run(go), downstream


def accepting(raw: str) -> Caller:
    assert raw == TOKEN
    return CALLER


def test_a_good_token_passes_with_its_caller_in_that_requests_scope_only() -> None:
    response, downstream = drive(accepting, {"Authorization": f"Bearer {TOKEN}", "X-Other": "1"})
    assert (response.status_code, response.content) == (200, b"ok")
    (scope,) = downstream.scopes
    assert scope[CALLER_KEY] == CALLER
    # The token goes no further than the middleware.
    names = [name for name, _ in scope["headers"]]
    assert b"authorization" not in names and b"x-other" in names
    assert not any(TOKEN.encode() in value for _, value in scope["headers"])


def test_every_http_method_is_authenticated() -> None:
    for method in ("GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"):
        response, downstream = drive(accepting, None, method)
        assert response.status_code == 401 and downstream.scopes == [], method


def refusing(_raw: str) -> Caller:
    raise AuthenticationError


def test_every_refusal_has_the_same_status_headers_and_body() -> None:
    cases: list[Any] = [
        None,
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer"},
        {"Authorization": ""},
        {"Authorization": f"Bearer {TOKEN}"},  # refused by the account store
        [("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
    ]
    seen = set()
    for headers in cases:
        response, downstream = drive(refusing, headers)
        assert downstream.scopes == []
        seen.add((response.status_code, tuple(sorted(response.headers.items())), response.content))
    assert len(seen) == 1
    ((status, headers, body),) = seen
    assert status == 401 and body == UNAUTHORIZED_BODY
    assert dict(headers)["www-authenticate"] == "Bearer"
    assert dict(headers)["content-type"] == "application/json"


def test_an_ambiguous_request_with_two_authorization_headers_is_not_authenticated() -> None:
    seen: list[str] = []

    def record(raw: str) -> Caller:
        seen.append(raw)
        return CALLER

    response, _ = drive(record, [("Authorization", f"Bearer {TOKEN}"), ("authorization", "x")])
    assert response.status_code == 401 and seen == []


def test_an_unavailable_user_store_is_503_with_a_fixed_body_and_no_exception_text() -> None:
    def down(_raw: str) -> Caller:
        raise AccountStoreError

    def broken(_raw: str) -> Caller:
        raise RuntimeError("secret detail of a bug")

    for authenticate in (down, broken):
        response, downstream = drive(authenticate, {"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 503 and response.content == UNAVAILABLE_BODY
        assert "www-authenticate" not in response.headers and downstream.scopes == []
        assert b"secret" not in response.content


def test_the_account_store_is_asked_off_the_event_loop() -> None:
    threads: list[int] = []
    loop: list[int] = []

    def authenticate(_raw: str) -> Caller:
        threads.append(threading.get_ident())
        return CALLER

    async def go() -> None:
        loop.append(threading.get_ident())
        app = BearerTokenAuth(Downstream(), authenticate)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://t") as http:
            await http.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})

    anyio.run(go)
    assert threads and threads[0] != loop[0]


def test_two_requests_with_two_tokens_each_get_their_own_caller() -> None:
    callers = {"a" * 8: Caller(user_id=1, role="viewer", channel="mcp", token_id=1)}
    callers["b" * 8] = Caller(user_id=2, role="viewer", channel="mcp", token_id=2)
    downstream = Downstream()

    def authenticate(raw: str) -> Caller:
        import time

        time.sleep(0.05 if raw == "a" * 8 else 0.0)  # the first token answers last
        return callers[raw]

    async def go() -> None:
        app = BearerTokenAuth(downstream, authenticate)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://t") as http:

            async def one(raw: str) -> None:
                await http.post("/mcp", headers={"Authorization": f"Bearer {raw}"})

            async with anyio.create_task_group() as group:
                group.start_soon(one, "a" * 8)
                group.start_soon(one, "b" * 8)

    anyio.run(go)
    assert sorted(s[CALLER_KEY].token_id for s in downstream.scopes) == [1, 2]
    assert downstream.scopes[0][CALLER_KEY].token_id == 2  # the faster one went through first


def test_a_websocket_is_closed_and_never_reaches_the_app() -> None:
    sent: list[dict[str, Any]] = []
    downstream = Downstream()

    async def go() -> None:
        async def receive() -> MutableMapping[str, Any]:
            return {"type": "websocket.connect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            sent.append(dict(message))

        await BearerTokenAuth(downstream, accepting)(
            {"type": "websocket", "headers": []}, receive, send
        )

    anyio.run(go)
    assert sent == [{"type": "websocket.close", "code": 1008}] and downstream.scopes == []


# --- the Host and Origin check ---


def test_the_host_check_allows_localhost_and_the_configured_hosts_only() -> None:
    plain = transport_security()
    assert plain.enable_dns_rebinding_protection is True
    assert plain.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert plain.allowed_origins == ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    configured = transport_security(["makna.example.org", "10.0.0.5:8443"])
    assert "makna.example.org" in configured.allowed_hosts
    assert "makna.example.org:*" in configured.allowed_hosts
    assert "10.0.0.5:8443" in configured.allowed_hosts
    assert "10.0.0.5:*" not in configured.allowed_hosts  # a port was named: only that one
    assert "https://makna.example.org" in configured.allowed_origins
    assert "*" not in configured.allowed_hosts


# --- `[mcp]` of config/makna.toml ---

ENV = {"MAKNA_DATABASE_URL": "postgresql://u:p@localhost/makna", "MAKNA_SECRET_KEY": "k"}


def test_the_mcp_settings_default_to_localhost_only(tmp_path: Any) -> None:
    assert load_settings(ENV, config_path="no-such-file.toml").mcp.allowed_hosts == ()
    config = tmp_path / "makna.toml"
    config.write_text('[mcp]\nallowed_hosts = ["makna.example.org", "[::1]:8000", "app:8000"]\n')
    assert load_settings(ENV, config_path=config).mcp.allowed_hosts == (
        "makna.example.org",
        "[::1]:8000",
        "app:8000",
    )


@pytest.mark.parametrize(
    "host",
    ["*", "*.example.org", "http://makna.example.org", "makna.example.org/mcp", "", "a b", "a:b"],
)
def test_a_host_with_a_scheme_a_path_or_a_wildcard_is_a_settings_error(
    host: str, tmp_path: Any
) -> None:
    config = tmp_path / "makna.toml"
    config.write_text(f'[mcp]\nallowed_hosts = ["{host}"]\n')
    with pytest.raises(ConfigError, match="mcp.allowed_hosts"):
        load_settings(ENV, config_path=config)
