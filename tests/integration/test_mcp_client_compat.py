"""WP-22C criterion MCP-3, the official SDK client over stdio (integration tier, no language model).

The server is the real application on a free port (uvicorn in a thread of the test process, on the
integration database with the sample definitions indexed). The client is the `mcp` package's own
`stdio_client` and `ClientSession`, which starts `makna mcp` as a subprocess with `MAKNA_URL` and
`MAKNA_TOKEN` only, as an agent's MCP configuration would. The tools are called directly; nothing
calls a model.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sqlalchemy import Engine

from . import mcp_support as m
from .service_support import Users
from .test_mcp_bridge import BRIDGE, ROOT, only_what_the_bridge_needs

TOOL_NAMES = [
    "makna_search",
    "makna_get_definition",
    "makna_list_related",
    "makna_compile_query",
]


@pytest.fixture(scope="module")
def tokens(service_world: tuple[Engine, Users]) -> m.Tokens:
    return m.make_tokens(service_world[0])


@pytest.fixture(scope="module")
def server_url(service_world: tuple[Engine, Users]) -> Iterator[str]:
    """One running server for the module: the application on a free port."""
    engine, _ = service_world
    with m.real_server(m.make_app(engine)) as base:
        yield base


def payload(result: Any) -> dict[str, Any]:
    """The JSON object of a tool result, which must not be a tool error."""
    assert result.is_error is not True, m.text_of(result)
    parsed = json.loads(m.text_of(result))
    assert isinstance(parsed, dict)
    return parsed


def test_the_sdk_stdio_client_lists_the_four_tools_and_completes_search_definition_and_compile(
    service_world: tuple[Engine, Users],
    tokens: m.Tokens,
    server_url: str,
) -> None:
    env = only_what_the_bridge_needs(server_url, tokens.by_role["viewer"])
    params = StdioServerParameters(command=sys.executable, args=BRIDGE, env=env, cwd=ROOT)

    async def go() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == TOOL_NAMES
                for tool in listed.tools:
                    assert tool.annotations is not None
                    assert tool.annotations.read_only_hint is True

                found = payload(
                    await session.call_tool("makna_search", {"query": "kredit macet per wilayah"})
                )
                assert found["confidence"] == "high"
                hit_ids = [hit["id"] for hit in found["hits"]]
                assert "metric:npf_ratio" in hit_ids

                definition = payload(
                    await session.call_tool("makna_get_definition", {"id": "metric:npf_ratio"})
                )
                assert definition["id"] == "metric:npf_ratio"
                assert definition["status"] == "approved"
                assert definition["example_requests"]
                assert any(d["ref"] == "branch.region_name" for d in definition["dimensions"])

                compiled = payload(
                    await session.call_tool(
                        "makna_compile_query",
                        {
                            "metric": "npf_ratio",
                            "dimensions": ["branch.region_name"],
                            "grain": "quarter",
                        },
                    )
                )
                assert compiled["sql"].lstrip().upper().startswith(("WITH", "SELECT"))
                assert compiled["dialect"] == "postgres"
                assert compiled["metric"]["id"] == "metric:npf_ratio"
                assert any(c["role"] == "dimension" for c in compiled["columns"])

    anyio.run(go)
