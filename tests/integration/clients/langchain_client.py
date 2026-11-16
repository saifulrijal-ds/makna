"""WP-22C client 2 (MCP-3): `langchain-mcp-adapters` over Streamable HTTP, with no model.

Run by `test_mcp_client_langchain.py` in an isolated environment built from the exact pins of
`langchain_requirements.txt` (`langchain-mcp-adapters` 0.3.2 needs an MCP SDK below 2, so it never
enters the project's dependencies). Environment: `MAKNA_TEST_MCP_URL`, the endpoint (`.../mcp`),
and `MAKNA_TOKEN`, sent only as the `Authorization: Bearer` header. The script loads the tools,
checks their names, calls three of them directly, and prints one JSON object on standard output.
Any failure prints a line on standard error and exits 1; the token is never printed.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import sys
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

SERVER = "makna"
TOOL_NAMES = ["makna_search", "makna_get_definition", "makna_list_related", "makna_compile_query"]


def payload(content: Any) -> dict[str, Any]:
    """The JSON object of a tool result: one text block, as the adapter returns MCP content."""
    if not (isinstance(content, list) and len(content) == 1):
        raise AssertionError(f"expected one content block, got {content!r:.200}")
    block = content[0]
    if not (isinstance(block, dict) and block.get("type") == "text"):
        raise AssertionError(f"expected a text block, got {block!r:.200}")
    parsed = json.loads(block["text"])
    if not isinstance(parsed, dict):
        raise AssertionError("a tool result is not a JSON object")
    return parsed


async def exercise(url: str, token: str) -> dict[str, Any]:
    client = MultiServerMCPClient(
        {
            SERVER: {
                "transport": "streamable_http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        },
        handle_tool_errors=False,  # a tool error raises, so a failure cannot pass silently
    )
    tools = {tool.name: tool for tool in await client.get_tools()}
    names = sorted(tools)
    if names != sorted(TOOL_NAMES):
        raise AssertionError(f"tool names are {names}, expected {sorted(TOOL_NAMES)}")

    found = payload(await tools["makna_search"].ainvoke({"query": "kredit macet per wilayah"}))
    definition = payload(await tools["makna_get_definition"].ainvoke({"id": "metric:npf_ratio"}))
    compiled = payload(
        await tools["makna_compile_query"].ainvoke(
            {"metric": "npf_ratio", "dimensions": ["branch.region_name"], "grain": "quarter"}
        )
    )
    return {
        "tools": names,
        "search": found,
        "definition": definition,
        "compile": compiled,
        "versions": {
            "langchain_mcp_adapters": importlib.metadata.version("langchain-mcp-adapters"),
            "mcp": importlib.metadata.version("mcp"),
        },
    }


def first_leaf(error: BaseException) -> str:
    """The class name of the first exception that is not a group, recursing into nested groups."""
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    return type(error).__name__


def main() -> int:
    url = os.environ.get("MAKNA_TEST_MCP_URL", "").strip()
    token = os.environ.get("MAKNA_TOKEN", "").strip()
    if not url or not token:
        print("langchain client: MAKNA_TEST_MCP_URL and MAKNA_TOKEN must be set", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(exercise(url, token))
    except BaseExceptionGroup as group:
        # A task group wraps the real failure. Name its first leaf by class only: the text can
        # carry the URL or server output.
        print(
            f"langchain client failed: {type(group).__name__}, first leaf {first_leaf(group)}",
            file=sys.stderr,
        )
        return 1
    except Exception as error:  # any failure, reported without the token or a header
        print(f"langchain client failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
