"""WP-13 fix round 1 (integration tier): what the SDK serves besides the four tools, that every
refusal has a log row, and that two requests with two tokens never share a caller.

All of it goes through the real application with real tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

import anyio
import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import Engine, text

from . import mcp_support as m
from .service_support import Users
from .test_mcp_http import INITIALIZE, JSON_HEADERS, LEGACY, parse, rpc

MARKER = "zxqmarkerqxz"


@pytest.fixture(scope="module")
def tokens(service_world: tuple[Engine, Users]) -> m.Tokens:
    return m.make_tokens(service_world[0])


def all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Every line the server side logged. The test's own client runs in the same process and
    logs what it sends at debug level; that is not the server's log."""
    return " ".join(
        f"{r.getMessage()} {r.exc_text or ''} {r.__dict__.get('args', '')}"
        for r in caplog.records
        if not r.name.startswith(("mcp.client", "httpx2"))
    )


# --- nothing a caller sends reaches the server log through what the SDK serves besides tools ---


def test_prompts_resources_and_odd_requests_answer_with_fixed_text_and_leave_no_trace_in_the_log(
    service_world: tuple[Engine, Users], tokens: m.Tokens, caplog: pytest.LogCaptureFixture
) -> None:
    engine, _ = service_world
    caplog.set_level(logging.DEBUG)
    text_marker = f"{MARKER}\nINFO: forged log line"
    auth = {"Authorization": f"Bearer {tokens.by_role['viewer']}", **LEGACY}

    async def go() -> list[str]:
        answers: list[str] = []
        app = m.make_app(engine)
        async with m.serving(app):
            # The 2025 era, by plain posts.
            async with m.http_client(app, **JSON_HEADERS) as http:
                await http.post(
                    "/mcp", headers={"Authorization": auth["Authorization"]}, json=INITIALIZE
                )
                for message in (
                    rpc("prompts/get", {"name": text_marker}, 11),
                    rpc("resources/read", {"uri": f"file:///{MARKER}"}, 12),
                    rpc("resources/read", {"uri": text_marker}, 13),
                    rpc(
                        "completion/complete",
                        {
                            "ref": {"type": "ref/prompt", "name": text_marker},
                            "argument": {"name": "a", "value": text_marker},
                        },
                        14,
                    ),
                    rpc("resources/subscribe", {"uri": f"file:///{MARKER}"}, 15),
                    rpc("logging/setLevel", {"level": text_marker}, 16),
                    rpc("tools/call", {"name": text_marker, "arguments": {}}, 18),
                    rpc(
                        "tools/call",
                        {"name": "makna_search", "arguments": {"query": "x", text_marker: 1}},
                        19,
                    ),
                    rpc("notifications/message", {"level": text_marker}, None),
                ):
                    response = await http.post("/mcp", headers=auth, json=message)
                    answers.append(response.text)
            # The 2026 era, through the SDK's own client.
            async with m.mcp_client(app, tokens.by_role["viewer"]) as client:
                for ask in (
                    client.get_prompt(text_marker),
                    client.read_resource(f"file:///{MARKER}"),
                    client.call_tool(f"{MARKER}_tool", {}),
                    client.call_tool("makna_search", {"query": "x", text_marker: 1}),
                ):
                    try:
                        answers.append(str(await ask))
                    except MCPError as error:
                        answers.append(str(error))
            # A refused Host or Origin is told to the client; the server log does not repeat it.
            async with m.http_client(app, **JSON_HEADERS) as http:
                refused = await http.post(
                    "/mcp",
                    headers={
                        **auth,
                        "Host": f"{MARKER}.example",
                        "Origin": f"http://{MARKER}.example",
                    },
                    json=INITIALIZE,
                )
                assert refused.status_code == 421
        return answers

    answers = anyio.run(go)
    assert answers
    assert all(MARKER not in answer for answer in answers)
    assert MARKER not in all_log_text(caplog) and "forged log line" not in all_log_text(caplog)


def test_an_unknown_method_name_reaches_no_log_line_at_warning_or_above(
    service_world: tuple[Engine, Users], tokens: m.Tokens, caplog: pytest.LogCaptureFixture
) -> None:
    """The SDK names an unknown method in its own answer to the sender and in a debug line (the
    debug level is for developers); at the levels a server runs at, no log line holds it."""
    engine, _ = service_world
    caplog.set_level(logging.DEBUG)
    auth = {"Authorization": f"Bearer {tokens.by_role['viewer']}", **LEGACY}

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app, **JSON_HEADERS) as http:
                await http.post(
                    "/mcp", headers={"Authorization": auth["Authorization"]}, json=INITIALIZE
                )
                for message in (
                    rpc(f"{MARKER}/method", {"x": f"{MARKER}\nforged"}, 2),
                    rpc(f"notifications/{MARKER}", {"x": MARKER}, None),
                    rpc("notifications/cancelled", {"requestId": MARKER}, None),
                ):
                    await http.post("/mcp", headers=auth, json=message)

    anyio.run(go)
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert MARKER not in " ".join(f"{r.getMessage()} {r.exc_text or ''}" for r in loud)


def test_the_prompt_and_resource_answers_are_fixed_protocol_errors_in_both_eras(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    auth = {"Authorization": f"Bearer {tokens.by_role['viewer']}", **LEGACY}

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app, **JSON_HEADERS) as http:
                await http.post(
                    "/mcp", headers={"Authorization": auth["Authorization"]}, json=INITIALIZE
                )
                prompt = parse(
                    await http.post("/mcp", headers=auth, json=rpc("prompts/get", {"name": "a"}, 2))
                )
                resource = parse(
                    await http.post(
                        "/mcp", headers=auth, json=rpc("resources/read", {"uri": "file:///a"}, 3)
                    )
                )
            assert prompt["error"] == {"code": -32602, "message": "Makna serves no prompts."}
            assert resource["error"] == {"code": -32602, "message": "Makna serves no resources."}
            async with m.mcp_client(app, tokens.by_role["viewer"]) as client:
                with pytest.raises(MCPError, match="no prompts"):
                    await client.get_prompt("a")
                with pytest.raises(MCPError, match="no resources"):
                    await client.read_resource("file:///a")

    anyio.run(go)


# --- no server streams ---


def test_no_stream_can_be_opened_a_get_is_405_and_listen_is_refused(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    auth = {"Authorization": f"Bearer {tokens.by_role['viewer']}"}

    async def go() -> None:
        # Bounded: if a GET could open a stream, or listen were served, the call would wait for
        # ever. A regression must fail in seconds, not hang.
        with anyio.fail_after(10):
            await body()

    async def body() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app, **JSON_HEADERS) as http:
                assert (await http.get("/mcp", headers=LEGACY)).status_code == 401
                for method in ("GET", "DELETE", "PUT", "PATCH"):
                    response = await http.request(method, "/mcp", headers={**auth, **LEGACY})
                    assert response.status_code == 405, method
                    assert response.headers["allow"] == "POST"
            async with m.mcp_client(app, tokens.by_role["viewer"]) as client:
                with pytest.raises(MCPError):
                    async with client.listen(tools_list_changed=True):
                        raise AssertionError("a subscription stream was opened")

    anyio.run(go)


# --- I3: a refusal is an answer, and every answer has a log row (section 14) ---


def newest(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(text("SELECT coalesce(max(id), 0) FROM tool_call_log")).scalar_one()
        )


def rows_since(engine: Engine, since: int) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT tool, status, error_code, request, user_id, token_id, channel "
                    "FROM tool_call_log WHERE id > :s ORDER BY id"
                ),
                {"s": since},
            )
        )


def test_an_unknown_field_name_in_the_log_is_redacted_like_any_logged_string(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    before = newest(engine)

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.mcp_client(app, tokens.by_role["viewer"]) as client:
                for key in ("acct_1234567890123", "card4111111111111111", "plain_name"):
                    await client.call_tool("makna_search", {"query": "x", key: 1})

    anyio.run(go)
    stored = [r.request for r in rows_since(engine, before)]
    text_stored = json.dumps(stored)
    assert "1234567890123" not in text_stored and "4111" not in text_stored
    assert "[REDACTED]" in text_stored and "plain_name" in text_stored


def test_an_unknown_field_is_a_refusal_with_a_log_row_in_every_tool_and_no_value_is_kept(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, users = service_world
    before = newest(engine)

    async def go() -> list[dict[str, Any]]:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.mcp_client(app, tokens.by_role["viewer"]) as client:
                found = []
                for tool, arguments in (
                    ("makna_search", {"query": "pencairan", "time_from": MARKER}),
                    (
                        "makna_get_definition",
                        {"id": "metric:npf_ratio", "include_deprecated": MARKER},
                    ),
                    ("makna_list_related", {"id": "metric:npf_ratio", "relations": MARKER}),
                    ("makna_compile_query", {"metric": "npf_ratio", "time_from": MARKER}),
                ):
                    result = await client.call_tool(tool, arguments)
                    assert result.is_error is True
                    found.append(json.loads(m.text_of(result)))
                return found

    answers = anyio.run(go)
    for answer in answers:
        error = answer["error"]
        assert error["code"] == "INVALID_INPUT"
        assert MARKER not in json.dumps(answer)  # names, never values
    assert "time_from" in json.dumps(answers[0]) and "include_deprecated" in json.dumps(answers[1])
    assert "time_from" in json.dumps(answers[3])  # the compile service names the field
    rows = rows_since(engine, before)
    assert [(r.tool, r.status, r.error_code) for r in rows] == [
        ("search", "refused", "INVALID_INPUT"),
        ("get_definition", "refused", "INVALID_INPUT"),
        ("list_related", "refused", "INVALID_INPUT"),
        ("compile_query", "refused", "INVALID_INPUT"),
    ]
    for row in rows:
        assert (row.user_id, row.token_id, row.channel) == (
            users.ids["viewer"],
            tokens.ids["viewer"],
            "mcp",
        )
        assert MARKER not in json.dumps(row.request)  # the log keeps no value either
    assert rows[0].request == {"unknown_fields": ["time_from"]}


def test_an_unknown_tool_is_a_protocol_error_and_has_no_row(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    before = newest(engine)
    auth = {"Authorization": f"Bearer {tokens.by_role['viewer']}", **LEGACY}

    async def go() -> dict[str, Any]:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.http_client(app, **JSON_HEADERS) as http:
                await http.post(
                    "/mcp", headers={"Authorization": auth["Authorization"]}, json=INITIALIZE
                )
                return parse(
                    await http.post(
                        "/mcp",
                        headers=auth,
                        json=rpc("tools/call", {"name": f"x{MARKER}", "arguments": {}}, 2),
                    )
                )

    answer = anyio.run(go)
    assert "result" not in answer and answer["error"]["code"] == -32602
    assert MARKER not in json.dumps(answer)
    assert rows_since(engine, before) == []


# --- I2: two requests, two tokens, at the same instant ---


class Rendezvous:
    """Sits behind the authentication: every request waits here for one other request, so two
    clients that go in step are both authenticated and past the middleware, with their callers
    stored wherever the server stores them, before either tool looks for its caller."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.barrier = asyncio.Barrier(2)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        with anyio.fail_after(30):
            await self.barrier.wait()
        await self.app(scope, receive, send)


def test_two_requests_with_two_tokens_at_the_same_instant_each_keep_their_own_caller(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    """A caller kept anywhere shared (a module variable, a context that outlives the request)
    would be the second request's for both. Each pair of requests meets behind the middleware,
    and again inside the search."""
    engine, users = service_world
    app = m.make_app(engine)
    endpoint = next(r for r in app.router.routes if getattr(r, "path", "") == "/mcp").endpoint  # type: ignore[attr-defined]
    endpoint._app = Rendezvous(endpoint._app)
    in_search = threading.Barrier(2, timeout=30)
    services = app.state.services
    original_search = services._search.search

    def meet_in_search(*args: Any, **kwargs: Any) -> Any:
        in_search.wait()
        return original_search(*args, **kwargs)

    services._search.search = meet_in_search

    async def one(role: str, word: str) -> None:
        async with m.mcp_client(app, tokens.by_role[role]) as client:
            result = await client.call_tool("makna_search", {"query": f"pencairan {word}"})
            assert result.is_error is not True

    async def go() -> None:
        async with m.serving(app):
            async with anyio.create_task_group() as group:
                group.start_soon(one, "viewer", "firstbarrier")
                group.start_soon(one, "admin", "secondbarrier")

    try:
        anyio.run(go)
    finally:
        services._search.search = original_search
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT request->>'query', user_id, token_id FROM tool_call_log "
                "WHERE request->>'query' LIKE 'pencairan %barrier'"
            )
        ).all()
    assert sorted(tuple(r) for r in rows) == sorted(
        [
            ("pencairan firstbarrier", users.ids["viewer"], tokens.ids["viewer"]),
            ("pencairan secondbarrier", users.ids["admin"], tokens.ids["admin"]),
        ]
    )


# --- the rest of the budget: the brief definition ---


def test_a_brief_definition_is_under_6000_characters_for_every_approved_metric(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, _ = service_world
    with engine.connect() as connection:
        ids = [
            str(r[0])
            for r in connection.execute(
                text("SELECT id FROM semantic_object WHERE kind='metric' AND status='approved'")
            )
        ]
    sizes: dict[str, int] = {}

    async def go() -> None:
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.mcp_client(app, tokens.by_role["viewer"]) as client:
                for metric_id in ids:
                    result = await client.call_tool(
                        "makna_get_definition", {"id": metric_id, "detail": "brief"}
                    )
                    assert result.is_error is not True
                    sizes[metric_id] = len(m.text_of(result))

    anyio.run(go)
    assert len(sizes) >= 20 and max(sizes.values()) < 6_000, max(sizes.values())
