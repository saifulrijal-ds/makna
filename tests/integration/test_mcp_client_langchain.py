"""WP-22C criterion MCP-3, the second client: `langchain-mcp-adapters` over Streamable HTTP
(integration tier, no language model).

The adapter (0.3.2) needs an MCP SDK below 2, so it never enters the project's dependencies. The
test runs `clients/langchain_client.py` under `uv run --isolated` with the exact pins of
`clients/langchain_requirements.txt`, against the same uvicorn server the SDK client test uses.
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


def run_client(url: str, token: str) -> dict[str, Any]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail(
            "uv is not on PATH: the LangChain client runs under uv (CI has it)", pytrace=False
        )
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("MAKNA_") and name != "VIRTUAL_ENV"
    }
    env["MAKNA_TEST_MCP_URL"] = url
    env["MAKNA_TOKEN"] = token
    command = [
        uv,
        "run",
        "--isolated",
        "--no-project",
        "--python",
        python,
        "--with-requirements",
        str(REQUIREMENTS),
        "python",
        str(SCRIPT),
    ]
    try:
        completed = subprocess.run(
            command,
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"the LangChain client did not finish in {TIMEOUT_SECONDS} seconds", pytrace=False
        )
    assert token not in completed.stdout and token not in completed.stderr
    if completed.returncode != 0:
        pytest.fail(
            f"the LangChain client exited {completed.returncode}: {completed.stderr[-2000:]}",
            pytrace=False,
        )
    parsed = json.loads(completed.stdout.strip().splitlines()[-1])
    assert isinstance(parsed, dict)
    return parsed


def test_the_langchain_client_lists_the_four_tools_and_completes_search_definition_and_compile(
    tokens: m.Tokens, server_url: str
) -> None:
    out = run_client(f"{server_url}/mcp", tokens.by_role["viewer"])

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
