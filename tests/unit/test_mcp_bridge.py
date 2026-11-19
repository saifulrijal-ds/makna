"""WP-13 unit tier: the stdio bridge starts only with its two variables, opens no connection
without them, holds no service or database code, and `makna mcp` reaches it."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import pytest

from makna.cli.main import main
from makna.mcp import bridge
from makna.mcp.bridge import BridgeSettingsError, settings_from

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "mkn_" + "B" * 43
URL = "http://localhost:8000"


def test_the_url_gets_the_mcp_path_appended_once() -> None:
    for given in (URL, URL + "/", URL + "///", f"  {URL}  "):
        assert settings_from({"MAKNA_URL": given, "MAKNA_TOKEN": TOKEN}) == (URL + "/mcp", TOKEN)
    https = settings_from({"MAKNA_URL": "https://makna.example.org/base", "MAKNA_TOKEN": TOKEN})
    assert https[0] == "https://makna.example.org/base/mcp"


def test_a_url_that_already_ends_in_mcp_is_not_doubled() -> None:
    for given in (URL + "/mcp", URL + "/mcp/", URL + "//mcp//"):
        assert settings_from({"MAKNA_URL": given, "MAKNA_TOKEN": TOKEN})[0] == URL + "/mcp"
    deep = settings_from({"MAKNA_URL": "https://h.example/base/mcp", "MAKNA_TOKEN": TOKEN})
    assert deep[0] == "https://h.example/base/mcp"


@pytest.mark.parametrize(
    "given",
    ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000", "HTTP://LOCALHOST/x"],
)
def test_plain_http_is_accepted_for_localhost(given: str) -> None:
    assert settings_from({"MAKNA_URL": given, "MAKNA_TOKEN": TOKEN})[0].endswith("/mcp")


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ("http://makna.example.org", "must use https"),
        ("http://10.0.0.5:8000", "must use https"),
        ("http://app:8000", "must use https"),
        ("http://localhost.example.org", "must use https"),
        ("https://user:secret@makna.example.org", "must not hold a user name"),
        ("https://user@makna.example.org", "must not hold a user name"),
        ("http://user:secret@localhost:8000", "must not hold a user name"),
        ("https://", "is not a URL"),
    ],
)
def test_the_token_is_not_sent_in_clear_text_to_another_host_nor_with_user_info(
    given: str, message: str
) -> None:
    with pytest.raises(BridgeSettingsError, match=message) as raised:
        settings_from({"MAKNA_URL": given, "MAKNA_TOKEN": TOKEN})
    assert "secret" not in str(raised.value) and given not in str(raised.value)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "MAKNA_URL and MAKNA_TOKEN not set"),
        ({"MAKNA_URL": URL}, "MAKNA_TOKEN not set"),
        ({"MAKNA_URL": URL, "MAKNA_TOKEN": ""}, "MAKNA_TOKEN not set"),
        ({"MAKNA_URL": URL, "MAKNA_TOKEN": "  \t "}, "MAKNA_TOKEN not set"),
        ({"MAKNA_TOKEN": TOKEN}, "MAKNA_URL not set"),
        ({"MAKNA_URL": "   ", "MAKNA_TOKEN": TOKEN}, "MAKNA_URL not set"),
        ({"MAKNA_URL": "ftp://example.org", "MAKNA_TOKEN": TOKEN}, "MAKNA_URL must start with"),
        ({"MAKNA_URL": "localhost:8000", "MAKNA_TOKEN": TOKEN}, "MAKNA_URL must start with"),
        ({"MAKNA_URL": URL, "MAKNA_TOKEN": "a b"}, "MAKNA_TOKEN holds a character"),
        ({"MAKNA_URL": URL, "MAKNA_TOKEN": "tokén"}, "MAKNA_TOKEN holds a character"),
    ],
)
def test_a_missing_or_unusable_variable_is_named_and_no_value_is_printed(
    env: dict[str, str], message: str
) -> None:
    with pytest.raises(BridgeSettingsError, match=message) as raised:
        settings_from(env)
    for value in env.values():
        if value.strip():
            assert value not in str(raised.value)


def test_the_bridge_reads_only_its_two_variables() -> None:
    env = {
        "MAKNA_URL": URL,
        "MAKNA_TOKEN": TOKEN,
        "MAKNA_DATABASE_URL": "postgresql://not-read",
        "MAKNA_SECRET_KEY": "not-read",
    }
    assert settings_from(env) == (URL + "/mcp", TOKEN)
    source = (ROOT / "src" / "makna" / "mcp" / "bridge.py").read_text(encoding="utf-8")
    assert source.count("MAKNA_") == source.count("MAKNA_URL") + source.count("MAKNA_TOKEN")


def test_without_a_token_it_exits_2_with_one_line_naming_the_variable_before_any_connection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_connection(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("the bridge started to connect")

    monkeypatch.setattr(anyio, "run", no_connection)
    for env in ({"MAKNA_URL": URL}, {"MAKNA_URL": URL, "MAKNA_TOKEN": "  "}, {}):
        assert bridge.main(env) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        (line,) = captured.err.strip().splitlines()
        assert line.startswith("makna mcp: ") and "MAKNA_" in line and URL not in line


def test_the_command_line_wires_makna_mcp_to_the_bridge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("MAKNA_URL", "MAKNA_TOKEN", "MAKNA_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert main(["mcp"]) == 2
    assert "MAKNA_URL and MAKNA_TOKEN not set" in capsys.readouterr().err
    monkeypatch.setenv("MAKNA_URL", URL)
    assert main(["mcp"]) == 2
    assert capsys.readouterr().err.strip() == "makna mcp: MAKNA_TOKEN not set"


def test_no_command_line_option_takes_the_token_or_the_url(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["mcp", "--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "MAKNA_URL" in help_text and "MAKNA_TOKEN" in help_text and "/mcp" in help_text
    assert "--token" not in help_text and "--url" not in help_text
    for option in ("--token", "--url"):  # not even a hidden one
        with pytest.raises(SystemExit) as refused:
            main(["mcp", option, "x"])
        assert refused.value.code == 2
    capsys.readouterr()


# --- the bridge holds no service or database code ---

FORBIDDEN = (
    "makna.services",
    "makna.search",
    "makna.compiler",
    "makna.index",
    "makna.catalog",
    "makna.engine",
    "makna.definitions",
    "makna.config",
    "makna.mcp.server",
    "makna.mcp.http",
    "makna.app",
    "sqlalchemy",
    "psycopg",
    "fastapi",
)


def test_loading_the_bridge_in_a_fresh_interpreter_imports_no_service_or_database_code() -> None:
    code = (
        "import sys\n"
        "import makna.mcp.bridge\n"
        f"forbidden = {FORBIDDEN!r}\n"
        "loaded = sorted(\n"
        "    n for n in sys.modules if any(n == f or n.startswith(f + '.') for f in forbidden)\n"
        ")\n"
        "print(loaded)\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "[]"


def test_the_package_init_does_not_pull_in_the_server() -> None:
    tree = ast.parse((ROOT / "src" / "makna" / "mcp" / "__init__.py").read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]


def test_the_command_line_imports_the_bridge_lazily_and_never_the_server() -> None:
    source = (ROOT / "src" / "makna" / "cli" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in tree.body:  # module level only: the bridge is imported inside a function
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert not [name for name in imported if name.startswith("makna.mcp")]
    assert "from makna.mcp.bridge import main as bridge_main" in source
    assert "makna.mcp.server" not in source and "makna.mcp.http" not in source


# --- the launcher: `makna mcp` loads no service or database code ---


def test_the_makna_command_runs_the_bridge_without_loading_service_or_database_code() -> None:
    code = (
        "import sys\n"
        "sys.argv = ['makna', 'mcp']\n"
        "from makna.cli.entry import main\n"
        "status = main()\n"
        f"forbidden = {FORBIDDEN!r}\n"
        "loaded = sorted(\n"
        "    n for n in sys.modules if any(n == f or n.startswith(f + '.') for f in forbidden)\n"
        ")\n"
        "print(status, loaded)\n"
    )
    environment = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        env=environment,
    )
    assert result.stdout.strip() == "2 []", result.stdout + result.stderr  # exit 2, nothing loaded
    assert result.stderr.strip() == "makna mcp: MAKNA_URL and MAKNA_TOKEN not set"


def test_the_console_script_points_at_the_launcher_and_help_still_lists_mcp() -> None:
    import tomllib

    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert scripts["scripts"]["makna"] == "makna.cli.entry:main"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.argv=['makna','--help']; from makna.cli.entry import main; main()",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert result.returncode == 0 and "mcp" in result.stdout


# --- what the bridge prints and when the server is not there ---


def run_bridge(url: str, lines: list[str], wait: float) -> subprocess.CompletedProcess[str]:
    environment = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "MAKNA_URL": url, "MAKNA_TOKEN": TOKEN}
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; from makna.mcp.bridge import main; sys.exit(main())",
        ],
        env=environment,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write("".join(line + "\n" for line in lines))
    process.stdin.flush()
    try:
        out, err = process.communicate(timeout=wait)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        return subprocess.CompletedProcess(process.args, -9, out, err)
    return subprocess.CompletedProcess(process.args, process.returncode, out, err)


INITIALIZE = (
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
    '"capabilities":{},"clientInfo":{"name":"t","version":"1"}}}'
)


def test_a_server_that_cannot_be_reached_ends_the_bridge_with_one_fixed_line() -> None:
    import socket
    import time

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again: nothing listens there
    started = time.monotonic()
    result = run_bridge(f"http://127.0.0.1:{port}", [INITIALIZE], wait=30)
    assert result.returncode == 1, result.stderr
    assert result.stderr.strip().splitlines()[-1] == "makna mcp: cannot reach the Makna server"
    assert str(port) not in result.stderr and "ExceptionGroup" not in result.stderr
    assert time.monotonic() - started < 20


def test_an_exception_group_is_reported_by_its_first_leaf_class() -> None:
    group = ExceptionGroup("outer", [ExceptionGroup("inner", [ConnectionRefusedError("x")])])
    assert type(bridge._leaf(group)) is ConnectionRefusedError
    assert type(bridge._leaf(ValueError("y"))) is ValueError


def test_a_malformed_answer_from_the_server_is_not_printed_on_standard_error() -> None:
    marker = "SERVER_BODY_MARKER_zxq"
    with StubServer(lambda _body: (200, "application/json", "{not json " + marker)) as stub:
        result = run_bridge(stub.url, [INITIALIZE], wait=8)
    # (The SDK answers the client's request with a JSON-RPC parse error on standard output; that
    # is the protocol's own channel to the client. Standard error is the log.)
    assert marker not in result.stderr
    assert TOKEN not in result.stderr
    for line in result.stderr.strip().splitlines():
        assert line.startswith("makna mcp: ")  # only the bridge's own format, class names only


def test_the_library_loggers_print_level_name_and_exception_class_only() -> None:
    import io
    import logging

    saved = sys.stderr
    sys.stderr = captured = io.StringIO()
    try:
        bridge.quiet_library_logs()
        logger = logging.getLogger("mcp.client.streamable_http")
        try:
            raise ValueError("TEXT_MARKER")
        except ValueError:
            logger.exception("message %s", "ARG_MARKER")
        logger.warning("plain WARN_MARKER")
        logging.getLogger("httpx2").info("below the level")
    finally:
        sys.stderr = saved
        for name in bridge.LIBRARY_LOGGERS:
            logging.getLogger(name).handlers = []
            logging.getLogger(name).propagate = True
    lines = captured.getvalue().strip().splitlines()
    assert lines == [
        "makna mcp: ERROR mcp.client.streamable_http: ValueError",
        "makna mcp: WARNING mcp.client.streamable_http",
    ]


# --- the bridge forwards every kind of message, both ways ---


def test_every_message_goes_both_ways_requests_notifications_and_responses() -> None:
    seen: list[dict[str, Any]] = []

    def answer(body: dict[str, Any]) -> tuple[int, str, str]:
        seen.append(body)
        method, ident = body.get("method"), body.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "stub", "version": "1"},
            }
            reply = {"jsonrpc": "2.0", "id": ident, "result": result}
            return 200, "application/json", json.dumps(reply)
        if method == "tools/list":
            events = [
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"level": "info", "data": "N"},
                },
                {"jsonrpc": "2.0", "id": "srv-1", "method": "ping"},
                {"jsonrpc": "2.0", "id": ident, "result": {"tools": []}},
            ]
            return (
                200,
                "text/event-stream",
                "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events),
            )
        return 202, "application/json", ""

    lines = [
        INITIALIZE,
        '{"jsonrpc":"2.0","method":"notifications/initialized"}',
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}',
        '{"jsonrpc":"2.0","id":"srv-1","result":{}}',
    ]
    with StubServer(answer) as stub:
        result = run_bridge(stub.url, lines, wait=60)
    assert result.returncode == 0, result.stderr
    # To the server: the request, the notification, and the client's own response. The bridge
    # posts independent messages concurrently (the SDK starts a task for each request), and
    # MCP orders only a request before its answer, so the test asserts the set received.
    assert sorted(
        (b.get("method") or f"response:{b.get('id')}" for b in seen),
    ) == sorted(["initialize", "notifications/initialized", "tools/list", "response:srv-1"])
    # To the client: the answer, the server's notification, and the server's own request.
    printed = [json.loads(line) for line in result.stdout.splitlines()]
    methods = [m.get("method") for m in printed]
    assert "notifications/message" in methods and "ping" in methods
    assert {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}} in printed
    assert any(m.get("id") == 1 and "result" in m for m in printed)


def test_a_client_response_written_last_reaches_the_server_before_the_bridge_ends() -> None:
    """MCP-3: a response or notification is not a pending request, so the bridge must wait for
    its own post. The stub answers that post late; a bridge that stops when no request is
    pending would end first and the message would never be recorded."""
    import time

    seen: list[dict[str, Any]] = []

    def answer(body: dict[str, Any]) -> tuple[int, str, str]:
        if body.get("method") == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "stub", "version": "1"},
            }
            return (
                200,
                "application/json",
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}),
            )
        time.sleep(4)
        seen.append(body)
        return 202, "application/json", ""

    last = ['{"jsonrpc":"2.0","id":"srv-1","result":{}}']
    with StubServer(answer) as stub:
        result = run_bridge(stub.url, [INITIALIZE, *last], wait=60)
    assert result.returncode == 0, result.stderr
    assert [b.get("id") for b in seen] == ["srv-1"]


class StubServer:
    """A server for `/mcp` that records what is posted and answers as `answer` says."""

    def __init__(self, answer: Callable[[Any], tuple[int, str, str]]) -> None:
        self._answer = answer
        self._httpd: ThreadingHTTPServer | None = None

    def __enter__(self) -> StubServer:
        answer = self._answer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                status, kind, text = answer(body)
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __exit__(self, *exc: object) -> None:
        assert self._httpd is not None
        self._httpd.shutdown()
        self._httpd.server_close()
