"""WP-22C criterion MCP-3, the second client: `langchain-mcp-adapters` over Streamable HTTP
(integration tier, no language model).

The adapter (0.3.2) needs an MCP SDK below 2, so it never enters the project's dependencies. The
test builds an environment in its own temporary directory with `uv pip install --require-hashes`
from the pins of `clients/langchain_requirements.txt` (uv verifies every hash on that path;
`uv run --with-requirements` does not), then runs `clients/langchain_client.py` with that
environment's Python against the same uvicorn server the SDK client test uses.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from makna.services import accounts

from . import mcp_support as m
from .service_support import Users

CLIENTS = Path(__file__).with_name("clients")
REQUIREMENTS = CLIENTS / "langchain_requirements.txt"
SCRIPT = CLIENTS / "langchain_client.py"
ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = 300
TOOL_NAMES = ["makna_compile_query", "makna_get_definition", "makna_list_related", "makna_search"]


@pytest.fixture(scope="module")
def tokens(service_world: tuple[Engine, Users]) -> m.Tokens:
    return m.make_tokens(service_world[0])


@pytest.fixture(scope="module")
def server_url(service_world: tuple[Engine, Users]) -> Iterator[str]:
    """One running server for the module: the application on a free port."""
    engine, _ = service_world
    with m.real_server(m.make_app(engine)) as base:
        yield base


def pinned(name: str) -> str:
    """The version the requirements file pins for `name`."""
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}=="):
            return line.split("==", 1)[1].split()[0]
    raise AssertionError(f"{name} is not pinned in {REQUIREMENTS.name}")


def run(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{command[0]} did not finish in {TIMEOUT_SECONDS} seconds", pytrace=False)


def hashed_environment(uv: str, directory: Path) -> Path:
    """A new environment with exactly the hashed pins, installed with hash verification.

    Returns the environment's Python. The install fails on any hash that does not match."""
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    venv = directory / "venv"
    created = run([uv, "venv", "--quiet", "--python", python, str(venv)])
    if created.returncode != 0:
        pytest.fail(f"uv venv failed: {created.stderr[-1000:]}", pytrace=False)
    environment_python = venv / "bin" / "python"
    installed = run(
        [
            uv,
            "pip",
            "install",
            "--quiet",
            "--require-hashes",
            "--python",
            str(environment_python),
            "-r",
            str(REQUIREMENTS),
        ]
    )
    if installed.returncode != 0:
        pytest.fail(
            f"the pinned requirements did not install with hash checks: {installed.stderr[-2000:]}",
            pytrace=False,
        )
    return environment_python


def run_script(tmp_path: Path, url: str, token: str) -> subprocess.CompletedProcess[str]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv is not on PATH: the LangChain client needs uv (CI has it)", pytrace=False)
    environment_python = hashed_environment(uv, tmp_path)
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("MAKNA_") and name != "VIRTUAL_ENV"
    }
    env["MAKNA_TEST_MCP_URL"] = url
    env["MAKNA_TOKEN"] = token
    completed = run([str(environment_python), str(SCRIPT)], env=env)
    assert token not in completed.stdout and token not in completed.stderr
    return completed


def test_the_langchain_client_lists_the_four_tools_and_completes_search_definition_and_compile(
    tmp_path: Path, tokens: m.Tokens, server_url: str
) -> None:
    completed = run_script(tmp_path, f"{server_url}/mcp", tokens.by_role["viewer"])
    if completed.returncode != 0:
        pytest.fail(
            f"the LangChain client exited {completed.returncode}: {completed.stderr[-2000:]}",
            pytrace=False,
        )
    out: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])

    assert out["tools"] == TOOL_NAMES
    assert out["versions"] == {
        "langchain_mcp_adapters": pinned("langchain-mcp-adapters"),
        "mcp": pinned("mcp"),
    }

    found = out["search"]
    assert found["confidence"] == "high"
    assert "metric:npf_ratio" in [hit["id"] for hit in found["hits"]]

    definition = out["definition"]
    assert definition["id"] == "metric:npf_ratio"
    assert definition["status"] == "approved"
    assert definition["example_requests"]
    assert any(d["ref"] == "branch.region_name" for d in definition["dimensions"])

    compiled = out["compile"]
    assert compiled["sql"].lstrip().upper().startswith(("WITH", "SELECT"))
    assert compiled["dialect"] == "postgres"
    assert compiled["metric"]["id"] == "metric:npf_ratio"
    assert any(c["role"] == "dimension" for c in compiled["columns"])


def test_a_wrong_token_fails_and_names_the_leaf_exception_class(
    tmp_path: Path, server_url: str
) -> None:
    unknown = accounts.generate_token()
    completed = run_script(tmp_path, f"{server_url}/mcp", unknown)
    assert completed.returncode == 1
    assert completed.stdout == ""
    # The group is named, and so is its first leaf (the pinned adapter raises the HTTP status
    # error for a refused token), by class only: no text of the error, no token.
    assert completed.stderr.strip() == (
        "langchain client failed: ExceptionGroup, first leaf HTTPStatusError"
    )
    assert unknown not in completed.stderr
