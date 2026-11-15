"""WP-13 acceptance criteria 1 to 4 and 6 (integration tier), with the SDK's own client.

The tools run against the service world of WP-12 (sample catalog and definitions, hash
embeddings). Each criterion is checked in process (the SDK's memory transport, same server) and,
where it matters, over the real HTTP path of `create_app` with a real token.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from sqlalchemy import Engine, text

from makna.mcp import texts
from makna.mcp.server import TOOLS

from . import mcp_support as m
from .service_support import Users

SNAPSHOT = Path(__file__).with_name("mcp_tools_snapshot.txt")
TOOL_NAMES = ["makna_search", "makna_get_definition", "makna_list_related", "makna_compile_query"]

# The 20 question texts of docs/EVALS.md section 2.5, copied.
QUESTIONS = [
    "How much did we disburse in June 2026?",
    "Total pencairan tahun ini untuk tiap wilayah?",
    "What is our NPF ratio right now?",
    "Rasio kredit macet tiap wilayah pada bulan lalu?",
    "Show the monthly NPF ratio trend for car-backed financing in the first half of 2026.",
    "Outstanding principal by DPD bucket at the end of June 2026",
    "Berapa sisa pokok per kuartal sepanjang 2025?",
    "Show the collection rate for each region in the last quarter.",
    "Tingkat penagihan bulanan untuk produk syariah, semester 1 2026",
    "Approval rate by product category in Q1 2026",
    "Berapa nasabah baru bulan lalu?",
    "What is the average ticket size for sharia products this year?",
    "What share of our outstanding principal is restructured?",
    "Disbursement by branch last month",
    "How much did customers pay us in May 2026, by payment channel?",
    "Berapa NPF kita?",
    "How many contracts do we have?",
    "What is our net interest margin this year?",
    "NPF ratio by payment channel",
    "List the names and phone numbers of customers who are more than 90 days past due.",
]
SEARCH_BUDGET = 2_500
DEFINITION_BUDGET = 6_000


@pytest.fixture(scope="module")
def tokens(service_world: tuple[Engine, Users]) -> m.Tokens:
    return m.make_tokens(service_world[0])


def body(result: Any) -> dict[str, Any]:
    parsed = json.loads(m.text_of(result))
    assert isinstance(parsed, dict)
    return parsed


def run(
    world: tuple[Engine, Users], tokens: m.Tokens, check: Callable[[Client], Awaitable[None]]
) -> None:
    """Run `check` once in process and once over HTTP with the viewer's real token."""
    engine, users = world

    async def both() -> None:
        async with m.in_process_client(engine, users) as client:
            await check(client)
        app = m.make_app(engine)
        async with m.serving(app):
            async with m.mcp_client(app, tokens.by_role["viewer"]) as client:
                await check(client)

    anyio.run(both)


# --- criterion 1: exactly the four tools, read-only, wording reviewed ---


def test_listing_tools_returns_exactly_the_four_tools_each_marked_read_only(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    async def check(client: Client) -> None:
        listed = (await client.list_tools()).tools
        assert [tool.name for tool in listed] == TOOL_NAMES  # no run tool (D-21), nothing else
        for tool in listed:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False
        # No resource and no prompt either.
        assert (await client.list_resources()).resources == []
        assert (await client.list_prompts()).prompts == []

    run(service_world, tokens, check)


def test_descriptions_and_instructions_match_the_reviewed_snapshot(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    expected = SNAPSHOT.read_text(encoding="utf-8")

    async def check(client: Client) -> None:
        tools = (await client.list_tools()).tools
        assert client.instructions is not None
        assert m.render_snapshot(client.instructions, tools) == expected

    run(service_world, tokens, check)


def test_the_constants_are_the_snapshot_text() -> None:
    """The same comparison without a server: the wording lives in `makna.mcp.texts`."""
    expected = SNAPSHOT.read_text(encoding="utf-8")
    for tool in TOOLS:
        assert f"## {tool.name}\ndescription:\n{tool.description}\n" in expected
    assert f"## instructions\n{texts.INSTRUCTIONS}\n" in expected


# --- criterion 2: the full flow ---


async def flow(client: Client) -> None:
    found = body(await client.call_tool("makna_search", {"query": "kredit macet per wilayah"}))
    top = next(hit for hit in found["hits"] if hit["kind"] == "metric")
    definition = body(await client.call_tool("makna_get_definition", {"id": top["id"]}))
    assert definition["id"] == top["id"] and definition["status"] == "approved"
    assert definition["example_requests"], "a definition carries its example requests"
    name = top["id"].removeprefix("metric:")
    choices = [
        d["ref"]
        for d in definition["dimensions"]
        if d["type"] == "categorical" and "masked" not in d
    ]
    request = {"metric": name, "dimensions": [choices[0]]}
    compiled = body(await client.call_tool("makna_compile_query", request))
    assert compiled["sql"].lstrip().upper().startswith(("WITH", "SELECT"))
    assert compiled["dialect"] == "postgres"
    assert compiled["metric"]["id"] == top["id"]
    assert compiled["explanation"]
    assert any(c["role"] == "dimension" for c in compiled["columns"])
    # The example request of the definition compiles as it stands.
    example = definition["example_requests"][0]["request"]
    again = body(await client.call_tool("makna_compile_query", {"metric": name, **example}))
    assert again["sql"]


async def npf_flow(client: Client) -> None:
    definition = body(await client.call_tool("makna_get_definition", {"id": "metric:npf_ratio"}))
    assert definition["example_requests"] and definition["rules"]
    assert any(d["ref"] == "branch.region_name" for d in definition["dimensions"])
    compiled = body(
        await client.call_tool(
            "makna_compile_query",
            {"metric": "npf_ratio", "dimensions": ["branch.region_name"], "grain": "quarter"},
        )
    )
    assert compiled["metric"]["id"] == "metric:npf_ratio"
    related = body(await client.call_tool("makna_list_related", {"id": "metric:npf_ratio"}))
    assert {item["relation"] for item in related["related"]} >= {"input_of"}


def test_the_full_flow_passes_in_process_and_over_http(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    run(service_world, tokens, flow)
    run(service_world, tokens, npf_flow)


# --- criterion 3: size budgets ---


def approved_metric_ids(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id FROM semantic_object WHERE kind = 'metric' AND status = 'approved' "
                "ORDER BY id"
            )
        )
        return [str(row[0]) for row in rows]


def test_search_output_is_within_budget_for_the_twenty_questions(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    sizes: dict[str, int] = {}

    async def check(client: Client) -> None:
        for question in QUESTIONS:
            result = await client.call_tool("makna_search", {"query": question})
            assert result.is_error is not True
            sizes[question] = len(m.text_of(result))

    run(service_world, tokens, check)
    assert len(sizes) == 20
    assert max(sizes.values()) <= SEARCH_BUDGET, sorted(sizes.items(), key=lambda i: -i[1])[:3]


# --- criterion 4: errors ---


def test_a_compile_error_is_a_tool_error_with_code_message_and_details(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    async def check(client: Client) -> None:
        result = await client.call_tool(
            "makna_compile_query",
            {"metric": "npf_ratio", "dimensions": ["payment.payment_channel"]},
        )
        assert result.is_error is True
        error = body(result)["error"]
        assert set(error) == {"code", "message", "details"}
        assert error["code"] == "DIMENSION_NOT_AVAILABLE"
        assert "branch.region_name" in error["details"]["available_dimensions"]
        assert error["message"]

    run(service_world, tokens, check)


def test_service_errors_are_tool_errors_in_the_same_shape(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    async def check(client: Client) -> None:
        result = await client.call_tool("makna_get_definition", {"id": "metric:npf_ratoi"})
        assert result.is_error is True
        error = body(result)["error"]
        assert error["code"] == "NOT_FOUND" and "metric:npf_ratio" in error["details"]["close_ids"]
        result = await client.call_tool("makna_search", {"query": "NPF", "limit": 11})
        error = body(result)["error"]
        assert (error["code"], error["details"]["field"]) == ("INVALID_INPUT", "limit")
        result = await client.call_tool("makna_compile_query", {"metric": "no_such_metric"})
        assert body(result)["error"]["code"] == "UNKNOWN_METRIC"

    run(service_world, tokens, check)


# --- criterion 6: no tool accepts SQL, and none accepts an engine credential ---

FORBIDDEN_PROPERTIES = frozenset(
    {
        "sql", "query_sql", "statement", "raw_sql", "where",
        "password", "passwd", "secret", "token", "api_key", "apikey", "credential", "credentials",
        "dsn", "connection", "connection_string", "engine", "engine_url", "database_url",
        "db_url", "url", "host", "user", "username", "login",
    }
)  # fmt: skip


def property_names(schema: Any) -> set[str]:
    """Every property name at any depth of a JSON schema."""
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                found |= set(value)
            found |= property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= property_names(item)
    return found


def test_no_tool_input_schema_has_a_sql_or_credential_property(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    async def check(client: Client) -> None:
        for tool in (await client.list_tools()).tools:
            names = {name.lower() for name in property_names(tool.input_schema)}
            assert names, tool.name
            assert not names & FORBIDDEN_PROPERTIES, (tool.name, names & FORBIDDEN_PROPERTIES)
            assert tool.input_schema["additionalProperties"] is False

    run(service_world, tokens, check)


def test_a_sql_argument_is_refused_before_any_service_runs(
    service_world: tuple[Engine, Users], tokens: m.Tokens
) -> None:
    async def check(client: Client) -> None:
        for tool, arguments in (
            ("makna_compile_query", {"metric": "npf_ratio", "sql": "SELECT 1"}),
            ("makna_search", {"query": "NPF", "sql": "SELECT 1"}),
        ):
            result = await client.call_tool(tool, arguments)
            error = body(result)["error"]
            assert result.is_error is True and error["code"] == "INVALID_INPUT"
            assert "SELECT" not in m.text_of(result)

    run(service_world, tokens, check)
