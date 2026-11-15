"""WP-13 criterion 7 (integration tier): the stdio bridge `makna mcp`.

The server is the real application on a free port (uvicorn in a thread of the test process, on
the integration database). The bridge is a separate process started like an MCP client would
start it, with an environment that holds `MAKNA_URL` and `MAKNA_TOKEN` and what Python needs, and
no `MAKNA_DATABASE_URL`.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters, get_default_environment
from sqlalchemy import Engine, text

from makna.services import accounts

from . import mcp_support as m
from .service_support import Users

BRIDGE = [
    "-c",
    "import sys; sys.argv = ['makna', 'mcp']; from makna.cli.entry import main; sys.exit(main())",
]
REFUSED = "makna mcp: the Makna server refused the token"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tokens(service_world: tuple[Engine, Users]) -> m.Tokens:
    return m.make_tokens(service_world[0])


def only_what_the_bridge_needs(url: str | None, token: str | None) -> dict[str, str]:
    """`MAKNA_URL`, `MAKNA_TOKEN`, and what Python needs to start: nothing of Makna's."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
    for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "LANG", "LC_ALL"):
        if name in os.environ:
            env[name] = os.environ[name]
    if url is not None:
        env["MAKNA_URL"] = url
    if token is not None:
        env["MAKNA_TOKEN"] = token
    assert not [
        name
        for name in env
        if name.startswith("MAKNA_") and name not in ("MAKNA_URL", "MAKNA_TOKEN")
    ]
    return env


def start(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, *BRIDGE],
        env=env,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def send(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def receive(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    line = process.stdout.readline()
    assert line, "the bridge closed its output"
    parsed = json.loads(line)
    assert isinstance(parsed, dict)
    return parsed


def last_log_rows(engine: Engine, since: int) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT tool, channel, user_id, token_id, status FROM tool_call_log "
                    "WHERE id > :since ORDER BY id"
                ),
                {"since": since},
            )
        )


def newest_log_id(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(text("SELECT coalesce(max(id), 0) FROM tool_call_log")).scalar_one()
        )


def test_the_bridge_refuses_to_start_without_a_token_and_opens_no_connection(
    service_world: tuple[Engine, Users],
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        for token in (None, "", "   "):
            process = start(only_what_the_bridge_needs(url, token))
            out, err = process.communicate(timeout=60)
            assert process.returncode == 2
            assert out == ""
            assert err.strip().splitlines() == ["makna mcp: MAKNA_TOKEN not set"]
        with pytest.raises(BlockingIOError):  # nobody connected
            listener.accept()
    process = start(only_what_the_bridge_needs(None, None))
    out, err = process.communicate(timeout=60)
    assert (process.returncode, out) == (2, "")
    assert err.strip() == "makna mcp: MAKNA_URL and MAKNA_TOKEN not set"


def test_the_bridge_completes_initialize_list_and_search_with_no_database_url_and_is_logged(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, users = service_world
    secret = tokens.by_role["viewer"]
    sink = m.Sink()
    before = newest_log_id(engine)
    app = m.make_app(engine)
    with m.real_server(app, sink) as base:
        env = only_what_the_bridge_needs(base, secret)
        assert "MAKNA_DATABASE_URL" not in env
        # The SDK's stdio client adds only a few safe variables of this process to `env`.
        assert not [name for name in get_default_environment() if name.startswith("MAKNA_")]
        params = StdioServerParameters(command=sys.executable, args=BRIDGE, env=env, cwd=ROOT)

        async def go() -> None:
            async with Client(params) as client:
                assert client.instructions is not None
                assert client.instructions.startswith("Makna holds")
                listed = await client.list_tools()
                assert [t.name for t in listed.tools] == [
                    "makna_search",
                    "makna_get_definition",
                    "makna_list_related",
                    "makna_compile_query",
                ]
                found = await client.call_tool("makna_search", {"query": "pencairan"})
                assert found.is_error is not True
                assert json.loads(m.text_of(found))["hits"]
                refused = await client.call_tool("makna_compile_query", {"metric": "nope"})
                assert json.loads(m.text_of(refused))["error"]["code"] == "UNKNOWN_METRIC"

        anyio.run(go)
    rows = last_log_rows(engine, before)
    assert [(r.tool, r.status) for r in rows] == [
        ("search", "ok"),
        ("compile_query", "refused"),
    ]
    for row in rows:  # the calls are logged with the token's user, token, and channel
        assert (row.channel, row.user_id, row.token_id) == (
            "mcp",
            users.ids["viewer"],
            tokens.ids["viewer"],
        )
    # The token is in no line the server logged, uvicorn's access log and debug lines included.
    assert sink.lines, "debug logging was on"
    assert not [line for line in sink.lines if secret in line or "Bearer" in line]


def test_a_client_of_the_2025_protocol_works_through_the_bridge(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    engine, users = service_world
    before = newest_log_id(engine)
    with m.real_server(m.make_app(engine)) as base:
        process = start(only_what_the_bridge_needs(base, tokens.by_role["curator"]))
        try:
            send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert receive(process)["result"]["protocolVersion"] == "2025-06-18"
            send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            assert len(receive(process)["result"]["tools"]) == 4
            send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "makna_search", "arguments": {"query": "kredit macet"}},
                },
            )
            answer = receive(process)
            assert answer["id"] == 3 and answer["result"]["isError"] is False
            assert process.stdin is not None
            process.stdin.close()  # the client is done: the bridge ends by itself, status 0
            assert process.wait(timeout=60) == 0
        finally:
            process.kill()
            process.communicate()
    rows = last_log_rows(engine, before)
    assert [(r.tool, r.channel, r.user_id, r.token_id) for r in rows] == [
        ("search", "mcp", users.ids["curator"], tokens.ids["curator"])
    ]


def test_a_401_ends_the_bridge_with_a_fixed_line_and_prints_neither_token_nor_body(
    service_world: tuple[Engine, Users],
) -> None:
    engine, _ = service_world
    revoked = accounts.create_token(engine, "viewer@example.com", "revoked before the bridge")
    accounts.revoke_token(engine, revoked.id)
    with m.real_server(m.make_app(engine)) as base:
        process = start(only_what_the_bridge_needs(base, revoked.token))
        try:
            send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            out, err = process.communicate(timeout=60)
        finally:
            process.kill()
    assert process.returncode == 1
    assert err.strip().splitlines()[-1] == REFUSED
    for stream in (out, err):
        assert revoked.token not in stream
        assert "UNAUTHENTICATED" not in stream and "Bearer" not in stream
