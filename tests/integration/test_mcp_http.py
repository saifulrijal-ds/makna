"""WP-13 criterion 5 and the rest of the HTTP boundary (integration tier).

Every request to `/mcp` is authenticated before the SDK sees it, with a real token from the
account store, on the real application of `create_app`. The calls below go through the ASGI app
with httpx (so the same code runs as under uvicorn); `test_mcp_bridge.py` uses a real server.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio
import httpx2
import pytest
from sqlalchemy import Engine, text

from makna.app import create_app
from makna.mcp.http import UNAUTHORIZED_BODY, UNAVAILABLE_BODY
from makna.services import accounts

from . import mcp_support as m
from .service_support import Users

JSON_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method: str, params: dict[str, Any] | None = None, id_: int | None = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        message["id"] = id_
    if params is not None:
        message["params"] = params
    return message


INITIALIZE = rpc(
    "initialize",
    {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
)
LEGACY = {"MCP-Protocol-Version": "2025-06-18"}


def search_call(query: str, id_: int = 2) -> dict[str, Any]:
    return rpc("tools/call", {"name": "makna_search", "arguments": {"query": query}}, id_)


def parse(response: httpx2.Response) -> dict[str, Any]:
    """The JSON-RPC message of a response, whether it came as JSON or as one SSE event."""
    content = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in content.splitlines() if line.startswith("data:")]
        content = data[-1]
    parsed = json.loads(content)
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture(scope="module")
def tokens(service_world: tuple[Engine, Users]) -> m.Tokens:
    return m.make_tokens(service_world[0])


def fingerprint(response: httpx2.Response) -> tuple[Any, ...]:
    return (response.status_code, tuple(sorted(response.headers.items())), response.content)


# --- criterion 5: 401 without a token, with a revoked token, with a malformed token ---


def test_http_without_a_token_with_a_revoked_token_and_with_a_malformed_token_is_401(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    revoked = accounts.create_token(engine, "viewer@example.com", "to revoke")
    accounts.revoke_token(engine, revoked.id)
    user = accounts.create_user(
        engine, "gone@example.com", "Gone", "viewer", [], "a long enough password"
    )
    inactive = accounts.create_token(engine, user.email, "inactive user")
    accounts.deactivate_user(engine, user.email)
    good = tokens.by_role["viewer"]
    bearer = "Bearer "
    cases: dict[str, dict[str, str]] = {
        "no header": {},
        "another scheme": {"Authorization": f"Basic {good}"},
        "no scheme": {"Authorization": good},
        "empty value": {"Authorization": ""},
        "scheme only": {"Authorization": "Bearer"},
        "scheme and spaces": {"Authorization": "Bearer    "},
        "malformed": {"Authorization": bearer + "not-a-token"},
        "too long": {"Authorization": bearer + "mkn_" + "A" * 5000},
        "inner whitespace": {"Authorization": f"Bearer {good[:10]} {good[10:]}"},
        "unknown": {"Authorization": bearer + accounts.generate_token()},
        "revoked": {"Authorization": bearer + revoked.token},
        "inactive user": {"Authorization": bearer + inactive.token},
    }

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            seen: dict[str, tuple[Any, ...]] = {}
            async with m.http_client(app, **JSON_HEADERS) as http:
                for name, headers in cases.items():
                    response = await http.post("/mcp", headers=headers, json=INITIALIZE)
                    assert response.status_code == 401, name
                    assert response.headers["www-authenticate"] == "Bearer", name
                    assert response.content == UNAUTHORIZED_BODY, name
                    seen[name] = fingerprint(response)
                # Not one detail tells the cases apart: status, headers, and body are equal.
                assert len(set(seen.values())) == 1
                # A good token is accepted by the same application.
                ok = await http.post(
                    "/mcp", headers={"Authorization": bearer + good}, json=INITIALIZE
                )
                assert ok.status_code == 200

    anyio.run(go)


def test_every_http_method_is_authenticated_before_the_sdk_sees_it(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app, **JSON_HEADERS) as http:
                for method in ("GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"):
                    for path in ("/mcp", "/mcp/"):
                        response = await http.request(method, path, json=INITIALIZE)
                        assert response.status_code == 401, (method, path)
                        assert response.headers["www-authenticate"] == "Bearer"
                        if method != "HEAD":
                            assert response.content == UNAUTHORIZED_BODY

    anyio.run(go)


def test_a_token_revoked_between_two_calls_is_refused_on_the_next(
    service_world: tuple[Engine, Users],
) -> None:
    engine, _ = service_world
    token = accounts.create_token(engine, "viewer@example.com", "revoked in between")
    headers = {"Authorization": f"Bearer {token.token}", "MCP-Protocol-Version": "2025-06-18"}

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app, **JSON_HEADERS) as http:
                first = await http.post("/mcp", headers=headers, json=search_call("pencairan"))
                assert first.status_code == 200 and not parse(first)["result"]["isError"]
                accounts.revoke_token(engine, token.id)
                second = await http.post("/mcp", headers=headers, json=search_call("pencairan"))
                assert second.status_code == 401 and second.content == UNAUTHORIZED_BODY

    anyio.run(go)


def test_scheme_case_and_surrounding_whitespace_do_not_matter(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    good = tokens.by_role["viewer"]

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app, **JSON_HEADERS) as http:
                for value in (f"bearer {good}", f"BEARER   {good}  ", f"  Bearer {good}"):
                    response = await http.post(
                        "/mcp", headers={"Authorization": value}, json=INITIALIZE
                    )
                    assert response.status_code == 200, value[:12]
                # Two Authorization headers are ambiguous: refused, even when one is good.
                response = await http.post(
                    "/mcp",
                    headers=[("Authorization", f"Bearer {good}"), ("Authorization", "Bearer x")],
                    json=INITIALIZE,
                )
                assert response.status_code == 401

    anyio.run(go)


def test_a_user_store_outage_is_503_with_a_fixed_body_not_401(
    service_world: tuple[Engine, Users],
) -> None:
    engine, _ = service_world
    down = engine.url.set(database="makna_no_such_database_for_the_outage_test")
    app = create_app(m.make_settings(down))
    token = accounts.generate_token()

    async def go() -> None:
        async with m.serving(app):
            async with m.http_client(app, token, **JSON_HEADERS) as http:
                response = await http.post("/mcp", json=INITIALIZE)
                assert response.status_code == 503
                assert response.content == UNAVAILABLE_BODY
                assert "www-authenticate" not in response.headers
                # A malformed token never reaches the database: still 401, not 503.
                response = await http.post(
                    "/mcp", headers={"Authorization": "Bearer nope"}, json=INITIALIZE
                )
                assert response.status_code == 401

    anyio.run(go)


# --- the mount ---


def test_the_endpoint_answers_at_mcp_without_a_redirect_and_healthz_stays_open(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    headers = {"Authorization": f"Bearer {tokens.by_role['viewer']}", **JSON_HEADERS}

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app) as http:
                assert (await http.get("/healthz")).json() == {"status": "ok"}
                for path in ("/mcp", "/mcp/"):
                    response = await http.post(path, headers=headers, json=INITIALIZE)
                    assert response.status_code == 200, path  # not 307
                    assert parse(response)["result"]["serverInfo"]["name"] == "makna"
                # Nothing else of the SDK is mounted: no metadata, no registration endpoint.
                for path in (
                    "/mcp/mcp",
                    "/.well-known/oauth-protected-resource",
                    "/.well-known/oauth-authorization-server",
                    "/register",
                    "/authorize",
                    "/token",
                ):
                    assert (await http.get(path, headers=headers)).status_code == 404, path

    anyio.run(go)


def test_building_the_application_opens_no_database_connection(
    service_world: tuple[Engine, Users],
) -> None:
    engine, _ = service_world
    down = engine.url.set(database="makna_no_such_database_for_the_start_test")
    app = create_app(m.make_settings(down))  # no database there; building must not mind

    async def go() -> None:
        async with m.serving(app):
            async with m.http_client(app) as http:
                assert (await http.get("/healthz")).status_code == 200

    anyio.run(go)


# --- host protection (DNS rebinding) stays on ---


def test_the_sdks_host_and_origin_checks_stay_on(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    auth = {"Authorization": f"Bearer {tokens.by_role['viewer']}", **JSON_HEADERS}

    async def post(app: Any, host: str, **more: str) -> httpx2.Response:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=f"http://{host}") as http:
            return await http.post("/mcp", headers={**auth, **more}, json=INITIALIZE)

    async def go() -> None:
        # Nothing configured: localhost only.
        app = m.make_app(engine, allowed_hosts=())
        async with m.serving(app):
            assert (await post(app, "localhost:8000")).status_code == 200
            assert (await post(app, "127.0.0.1:8000")).status_code == 200
            assert (await post(app, m.HOST)).status_code == 421
            assert (await post(app, "evil.example")).status_code == 421
            # Authentication comes first: no token, no hint about the host check.
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://evil.example") as h:
                assert (
                    await h.post("/mcp", headers=JSON_HEADERS, json=INITIALIZE)
                ).status_code == 401
            assert (
                await post(app, "localhost:8000", Origin="http://evil.example")
            ).status_code == 403
            assert (
                await post(app, "localhost:8000", Origin="http://localhost:3000")
            ).status_code == 200
        # A configured host is allowed, with or without a port; others still are not.
        app = m.make_app(engine, allowed_hosts=("makna.example.org",))
        async with m.serving(app):
            assert (await post(app, "makna.example.org")).status_code == 200
            assert (await post(app, "makna.example.org:8443")).status_code == 200
            assert (
                await post(app, "makna.example.org", Origin="https://makna.example.org")
            ).status_code == 200
            assert (await post(app, "other.example.org")).status_code == 421

    anyio.run(go)


# --- stateless: nothing to steal or replay ---


def test_the_server_keeps_no_session_so_a_session_id_gives_nothing(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, users = service_world
    viewer = tokens.by_role["viewer"]
    admin = tokens.by_role["admin"]

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app, **JSON_HEADERS) as http:
                first = await http.post(
                    "/mcp", headers={"Authorization": f"Bearer {viewer}"}, json=INITIALIZE
                )
                assert "mcp-session-id" not in first.headers
                # An invented session id changes nothing: with no token it is 401, with a token
                # the call is served and acts as the token's user, whatever the id says.
                fake = {"Mcp-Session-Id": "0123456789abcdef", **LEGACY}
                refused = await http.post("/mcp", headers=fake, json=search_call("pencairan"))
                assert refused.status_code == 401
                other = {"Authorization": f"Bearer {admin}", **fake}
                served = await http.post("/mcp", headers=other, json=search_call("pencairan beta"))
                assert served.status_code == 200 and not parse(served)["result"]["isError"]

    anyio.run(go)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT user_id, token_id FROM tool_call_log "
                "WHERE request->>'query' = 'pencairan beta' ORDER BY id DESC LIMIT 1"
            )
        ).one()
    assert tuple(row) == (
        users.ids["admin"],
        tokens.ids["admin"],
    )  # the admin's token, not another session's


# --- the legacy handshake era works too ---


def test_a_client_of_the_2025_protocol_lists_and_calls_tools_over_plain_posts(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    headers = {"Authorization": f"Bearer {tokens.by_role['viewer']}"}

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app, **JSON_HEADERS) as http:
                init = parse(await http.post("/mcp", headers=headers, json=INITIALIZE))
                assert init["result"]["protocolVersion"] == "2025-06-18"
                assert init["result"]["instructions"].startswith("Makna holds")
                sent = await http.post(
                    "/mcp",
                    headers={**headers, **LEGACY},
                    json=rpc("notifications/initialized", None, None),
                )
                assert sent.status_code == 202
                listed = parse(
                    await http.post(
                        "/mcp", headers={**headers, **LEGACY}, json=rpc("tools/list", {}, 3)
                    )
                )
                assert [t["name"] for t in listed["result"]["tools"]] == [
                    "makna_search",
                    "makna_get_definition",
                    "makna_list_related",
                    "makna_compile_query",
                ]
                called = parse(
                    await http.post("/mcp", headers={**headers, **LEGACY}, json=search_call("npf"))
                )
                assert called["result"]["isError"] is False
                content = called["result"]["content"]
                assert json.loads(content[0]["text"])["hits"]

    anyio.run(go)


# --- who is calling: per request, never shared ---


def test_two_tokens_of_two_users_calling_at_once_each_get_their_own_caller(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, users = service_world
    calls = 12

    async def user_calls(app: Any, role: str, word: str) -> None:
        async with m.mcp_client(app, tokens.by_role[role]) as client:
            for _ in range(calls):
                result = await client.call_tool("makna_search", {"query": f"pencairan {word}"})
                assert result.is_error is not True

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with anyio.create_task_group() as group:
                group.start_soon(user_calls, app, "viewer", "alphaword")
                group.start_soon(user_calls, app, "curator", "betaword")

    anyio.run(go)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT request->>'query', user_id, token_id, channel, status FROM tool_call_log "
                "WHERE request->>'query' LIKE 'pencairan %word'"
            )
        ).all()
    expected = {
        "pencairan alphaword": (users.ids["viewer"], tokens.ids["viewer"]),
        "pencairan betaword": (users.ids["curator"], tokens.ids["curator"]),
    }
    assert sorted(set(r[0] for r in rows)) == sorted(expected)
    assert len(rows) == 2 * calls
    for query, user_id, token_id, channel, status in rows:
        assert (user_id, token_id) == expected[query], query
        assert (channel, status) == ("mcp", "ok")


def test_a_token_acts_as_a_viewer_whatever_its_users_role(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    """The admin's token cannot see a draft: the caller is a token's, so `effective_role` is
    `viewer` (D-4), and the permission matrix reads the agent-token column."""
    engine, _ = service_world

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.mcp_client(app, tokens.by_role["admin"]) as client:
                result = await client.call_tool(
                    "makna_get_definition", {"id": "metric:write_off_amount"}
                )
                assert result.is_error is True
                assert json.loads(m.text_of(result))["error"]["code"] == "NOT_FOUND"

    anyio.run(go)


# --- what the logs hold ---


def test_the_token_never_reaches_a_log_line_even_at_debug(
    service_world: tuple[Engine, Users], tokens: m.Tokens, caplog: pytest.LogCaptureFixture
) -> None:
    engine, _ = service_world
    secret = tokens.by_role["viewer"]
    wrong = accounts.generate_token()
    caplog.set_level(logging.DEBUG)

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.mcp_client(app, secret) as client:
                await client.list_tools()
                await client.call_tool("makna_search", {"query": "pencairan"})
                await client.call_tool("makna_compile_query", {"metric": "no_such_metric"})
            for bad in (wrong, "nope"):
                async with m.http_client(app, bad, **JSON_HEADERS) as http:
                    assert (await http.post("/mcp", json=INITIALIZE)).status_code == 401

    anyio.run(go)
    assert caplog.records, "debug logging was on, so something was logged"
    for record in caplog.records:
        line = record.getMessage() + (record.exc_text or "") + str(record.__dict__.get("args", ""))
        for needle in (secret, wrong, "Bearer ", "Authorization"):
            assert needle not in line, (record.name, needle)


def test_the_text_of_an_error_in_a_service_never_leaves_the_server(
    service_world: tuple[Engine, Users], tokens: m.Tokens, caplog: pytest.LogCaptureFixture
) -> None:
    """A database error carries the caller's own words as SQL parameters (the note
    `database-error-text-carries-the-query-parameters`). The real service turns it into
    INTERNAL, and the response and the log hold neither the words nor the error's text."""
    engine, _ = service_world
    marker = "zxqmarkerwordqxz"
    caplog.set_level(logging.DEBUG)
    broken = m.make_app(engine)
    original = broken.state.services._search.search

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"connection lost while searching {marker}")

    broken.state.services._search.search = failing

    async def go() -> dict[str, Any]:
        async with m.serving(broken):
            async with m.mcp_client(broken, tokens.by_role["viewer"]) as client:
                result = await client.call_tool("makna_search", {"query": marker})
                assert result.is_error is True
                parsed = json.loads(m.text_of(result))
                assert isinstance(parsed, dict)
                return parsed

    try:
        answer = anyio.run(go)
    finally:
        broken.state.services._search.search = original
    assert answer["error"]["code"] == "INTERNAL" and answer["error"]["details"] == {}
    assert marker not in json.dumps(answer) and "RuntimeError" not in json.dumps(answer)
    logged = " ".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert "RuntimeError" in logged  # the class is logged ...
    assert "connection lost" not in logged  # ... and nothing of its text


def test_a_get_or_delete_of_the_2026_era_is_405_after_authentication(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    modern = {"MCP-Protocol-Version": "2026-07-28"}
    auth = {"Authorization": f"Bearer {tokens.by_role['viewer']}"}

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app, **JSON_HEADERS) as http:
                for method in ("GET", "DELETE"):
                    refused = await http.request(method, "/mcp", headers=modern)
                    assert refused.status_code == 401, method  # authentication first
                    served = await http.request(method, "/mcp", headers={**auth, **modern})
                    assert served.status_code == 405 and served.headers["allow"] == "POST"

    anyio.run(go)
