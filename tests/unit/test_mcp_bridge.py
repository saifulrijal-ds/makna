"""WP-13 unit tier: the stdio bridge starts only with its two variables, opens no connection
without them, holds no service or database code, and `makna mcp` reaches it."""

from __future__ import annotations

import ast
import subprocess
import sys
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
