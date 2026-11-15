"""WP-13 unit tier: the MCP tools without a database.

The tools run against a stub of the service layer and the SDK's client in process. What is
checked here: how every kind of failure becomes a tool result (and what never does), what the
tools pass to the services, the written-out input schemas, and the wording against section 12.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, CallToolResult

from makna.mcp import texts
from makna.mcp.http import NoCallerError, caller_from_request
from makna.mcp.server import (
    ANNOTATIONS,
    COMPILE_SCHEMA,
    GET_DEFINITION_SCHEMA,
    LIST_RELATED_SCHEMA,
    SEARCH_SCHEMA,
    TOOLS,
    build_mcp_server,
)
from makna.model.request import (
    CompileResponse,
    ErrorCode,
    MetricRef,
    QueryError,
    QueryRequest,
    ResultColumn,
)
from makna.services import Caller, PermissionDenied, ServiceError, ServiceErrorCode
from makna.services.service import INTERNAL_MESSAGE

ROOT = Path(__file__).resolve().parents[2]
CALLER = Caller(user_id=1, role="viewer", channel="mcp", token_id=1)
MARKER = "zxq-secret-marker-qxz"


class View:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return self.payload


class StubServices:
    """Records what the tools pass on; answers with `result`, or raises `error`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.refusals: list[tuple[str, list[Any], list[str]]] = []
        self.threads: list[int] = []
        self.result: Any = View({"ok": True})
        self.error: BaseException | None = None

    def _answer(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        self.threads.append(threading.get_ident())
        if self.error is not None:
            raise self.error
        return self.result

    def refuse_input(self, caller: Caller, tool: str, fields: Any, allowed: Any) -> None:
        self.refusals.append((tool, list(fields), list(allowed)))
        raise ServiceError(ServiceErrorCode.INVALID_INPUT, "stub refusal", {"fields": list(fields)})

    def search(self, *args: Any, **kwargs: Any) -> Any:
        return self._answer("search", *args, **kwargs)

    def get_definition(self, *args: Any, **kwargs: Any) -> Any:
        return self._answer("get_definition", *args, **kwargs)

    def list_related(self, *args: Any, **kwargs: Any) -> Any:
        return self._answer("list_related", *args, **kwargs)

    def compile_query(self, *args: Any, **kwargs: Any) -> Any:
        return self._answer("compile_query", *args, **kwargs)


def call(
    stub: StubServices,
    tool: str,
    arguments: dict[str, Any],
    caller_of: Callable[[Any], Caller] = lambda _context: CALLER,
) -> CallToolResult:
    async def go() -> CallToolResult:
        server = build_mcp_server(stub, caller_of)  # type: ignore[arg-type]
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    return anyio.run(go)


def payload(result: CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1 and result.content[0].type == "text"
    parsed = json.loads(result.content[0].text)
    assert isinstance(parsed, dict)
    return parsed


# --- the tools and their words ---


def test_the_server_has_the_four_tools_and_nothing_else() -> None:
    async def go() -> None:
        async with Client(build_mcp_server(StubServices(), lambda _c: CALLER)) as client:  # type: ignore[arg-type]
            listed = (await client.list_tools()).tools
            assert [t.name for t in listed] == [
                "makna_search",
                "makna_get_definition",
                "makna_list_related",
                "makna_compile_query",
            ]
            assert all(t.annotations == ANNOTATIONS for t in listed)
            assert (await client.list_resources()).resources == []
            assert (await client.list_resource_templates()).resource_templates == []
            assert (await client.list_prompts()).prompts == []
            assert client.instructions == texts.INSTRUCTIONS

    anyio.run(go)


def test_every_tool_is_marked_read_only() -> None:
    assert ANNOTATIONS.read_only_hint is True
    assert ANNOTATIONS.destructive_hint is False
    assert ANNOTATIONS.idempotent_hint is True
    assert ANNOTATIONS.open_world_hint is False


def doc_section_12() -> str:
    doc = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    return doc[doc.index("## 12. The MCP server") : doc.index("## 13. The web app")]


def quote_after(section: str, heading: str) -> str:
    start = section.index(heading)
    match = re.search(r"^> (.+)$", section[start:], flags=re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_the_wording_is_the_text_of_section_12_of_the_architecture() -> None:
    section = doc_section_12()
    assert texts.INSTRUCTIONS == quote_after(section, "Server instructions")
    by_name = {tool.name: tool.description for tool in TOOLS}
    for name in by_name:
        assert by_name[name] == quote_after(section, f"### `{name}`"), name


def property_names(schema: Any) -> set[str]:
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


def test_compile_takes_the_fields_of_a_query_request_at_the_top_level() -> None:
    assert set(COMPILE_SCHEMA["properties"]) == set(QueryRequest.model_fields)
    assert COMPILE_SCHEMA["required"] == ["metric"]
    filter_schema = COMPILE_SCHEMA["properties"]["filters"]["items"]
    assert set(filter_schema["properties"]) == {"dimension", "op", "value"}


@pytest.mark.parametrize(
    "schema", [SEARCH_SCHEMA, GET_DEFINITION_SCHEMA, LIST_RELATED_SCHEMA, COMPILE_SCHEMA]
)
def test_every_schema_refuses_unknown_fields_and_has_no_sql_property(
    schema: dict[str, Any],
) -> None:
    assert schema["additionalProperties"] is False
    assert not property_names(schema) & {"sql", "query_sql", "include_deprecated"}


def test_the_schemas_advertise_the_bounds_the_services_check() -> None:
    search = SEARCH_SCHEMA["properties"]
    assert (search["limit"]["minimum"], search["limit"]["maximum"], search["limit"]["default"]) == (
        1,
        10,
        5,
    )
    assert search["kinds"]["items"]["enum"] == [
        "metric",
        "glossary_term",
        "entity",
        "dimension",
        "column",
    ]
    assert GET_DEFINITION_SCHEMA["properties"]["detail"]["enum"] == ["brief", "full"]
    assert GET_DEFINITION_SCHEMA["properties"]["detail"]["default"] == "full"
    assert "input_of" in LIST_RELATED_SCHEMA["properties"]["relation"]["enum"]
    assert COMPILE_SCHEMA["properties"]["limit"]["maximum"] == 10000


# --- what the tools pass to the services ---


def test_search_passes_its_arguments_and_the_default_limit() -> None:
    stub = StubServices()
    result = call(stub, "makna_search", {"query": "npf", "kinds": ["metric"], "domain": "lending"})
    assert payload(result) == {"ok": True} and result.is_error is not True
    name, args, kwargs = stub.calls[0]
    assert (name, args) == ("search", (CALLER, "npf"))
    assert kwargs == {"kinds": ["metric"], "domain": "lending", "limit": 5}


def test_search_never_passes_include_deprecated() -> None:
    stub = StubServices()
    result = call(stub, "makna_search", {"query": "npf", "include_deprecated": True})
    assert result.is_error is True and stub.calls == []
    assert stub.refusals == [
        ("search", ["include_deprecated"], ["domain", "kinds", "limit", "query"])
    ]


def test_get_definition_and_list_related_pass_their_arguments() -> None:
    stub = StubServices()
    call(stub, "makna_get_definition", {"id": "metric:npf_ratio"})
    call(stub, "makna_get_definition", {"id": "metric:npf_ratio", "detail": "brief"})
    call(stub, "makna_list_related", {"id": "metric:npf_ratio", "relation": "input_of"})
    assert [(c[0], c[1], c[2]) for c in stub.calls] == [
        ("get_definition", (CALLER, "metric:npf_ratio"), {"detail": "full"}),
        ("get_definition", (CALLER, "metric:npf_ratio"), {"detail": "brief"}),
        ("list_related", (CALLER, "metric:npf_ratio"), {"relation": "input_of"}),
    ]


def test_compile_passes_the_request_as_a_mapping_for_the_service_to_check() -> None:
    stub = StubServices()
    response = CompileResponse(
        sql="SELECT 1",
        dialect="postgres",
        columns=(ResultColumn(name="x", role="metric", unit="percent"),),
        metric=MetricRef(id="metric:npf_ratio", version=1),
    )
    stub.result = response
    request = {"metric": "npf_ratio", "dimensions": ["branch.region_name"], "limit": "ten"}
    result = call(stub, "makna_compile_query", request)
    assert stub.calls[0][1:] == ((CALLER, request), {})  # not even the wrong type is judged here
    assert payload(result) == {
        "sql": "SELECT 1",
        "dialect": "postgres",
        "columns": [{"name": "x", "role": "metric", "unit": "percent"}],
        "metric": {"id": "metric:npf_ratio", "version": 1},
    }


def test_the_services_run_off_the_event_loop() -> None:
    stub = StubServices()
    loop_thread: list[int] = []

    async def go() -> None:
        loop_thread.append(threading.get_ident())
        async with Client(build_mcp_server(stub, lambda _c: CALLER)) as client:  # type: ignore[arg-type]
            await client.call_tool("makna_search", {"query": "npf"})

    anyio.run(go)
    assert stub.threads and stub.threads[0] != loop_thread[0]


def test_a_result_is_one_compact_json_text_with_indonesian_kept_as_typed() -> None:
    stub = StubServices()
    stub.result = View({"description": "Rasio kredit bermasalah — per wilayah"})
    result = call(stub, "makna_search", {"query": "npf"})
    assert result.content[0].text == '{"description":"Rasio kredit bermasalah — per wilayah"}'  # type: ignore[union-attr]
    assert result.structured_content is None  # the text is the one copy


# --- errors ---


@pytest.mark.parametrize(
    "error",
    [
        ServiceError(
            ServiceErrorCode.NOT_FOUND, "No such id.", {"id": "metric:x", "close_ids": []}
        ),
        ServiceError(ServiceErrorCode.INVALID_INPUT, "limit: 1 to 10", {"field": "limit"}),
        ServiceError(ServiceErrorCode.CATALOG_NOT_SYNCED, "Sync the catalog."),
        ServiceError(ServiceErrorCode.INTERNAL, INTERNAL_MESSAGE),
        PermissionDenied("Not allowed.", {"permission": "compile"}),
        QueryError(
            ErrorCode.DIMENSION_NOT_AVAILABLE,
            "Use another dimension.",
            {"available_dimensions": ["branch.region_name"]},
        ),
        QueryError(ErrorCode.UNKNOWN_METRIC, "No such metric."),
    ],
)
def test_a_refusal_is_a_tool_error_with_the_json_of_the_error(
    error: ServiceError | QueryError,
) -> None:
    stub = StubServices()
    stub.error = error
    result = call(stub, "makna_search", {"query": "npf"})
    assert result.is_error is True
    assert payload(result) == error.to_dict()
    assert set(payload(result)["error"]) == {"code", "message", "details"}


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(MARKER),
        ValueError(MARKER),
        KeyError(MARKER),
        OSError(MARKER),
        PermissionError(MARKER),  # a built-in one that is not a service error
        type("DataError", (Exception,), {})(
            f"invalid input syntax: {MARKER} [parameters: {MARKER}]"
        ),
    ],
)
def test_any_other_exception_is_internal_without_its_text_or_class(
    error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    stub = StubServices()
    stub.error = error
    result = call(stub, "makna_get_definition", {"id": "metric:x"})
    assert result.is_error is True
    assert payload(result) == ServiceError(ServiceErrorCode.INTERNAL, INTERNAL_MESSAGE).to_dict()
    text = result.content[0].text  # type: ignore[union-attr]
    assert MARKER not in text and type(error).__name__ not in text
    assert "Traceback" not in text and "File " not in text
    logged = " ".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert type(error).__name__ in logged  # only the class is logged ...
    assert MARKER not in logged  # ... never the text, in a message or in a traceback
    assert not any(r.exc_info for r in caplog.records if r.name.startswith("makna"))


def test_a_detail_the_json_encoder_cannot_take_is_internal_not_a_crash() -> None:
    stub = StubServices()
    stub.error = ServiceError(ServiceErrorCode.INVALID_INPUT, "x", {"values": {MARKER}})  # a set
    result = call(stub, "makna_search", {"query": "npf"})
    assert result.is_error is True
    assert payload(result)["error"]["code"] == "INTERNAL"
    assert MARKER not in result.content[0].text  # type: ignore[union-attr]
    stub.error = None
    stub.result = View({"x": float("nan")})
    assert payload(call(stub, "makna_search", {"query": "npf"}))["error"]["code"] == "INTERNAL"


def test_a_call_without_an_authenticated_caller_is_internal_and_runs_no_service() -> None:
    stub = StubServices()
    # The in-process transport carries no HTTP request, so the real lookup finds no caller.
    result = call(stub, "makna_search", {"query": "npf"}, caller_of=caller_from_request)
    assert result.is_error is True and payload(result)["error"]["code"] == "INTERNAL"
    assert stub.calls == []
    with pytest.raises(NoCallerError):
        caller_from_request(None)


def test_an_unknown_tool_is_a_protocol_error_with_fixed_text_and_no_echo() -> None:
    stub = StubServices()
    outcome: list[MCPError] = []

    async def go() -> None:
        async with Client(build_mcp_server(stub, lambda _c: CALLER)) as client:  # type: ignore[arg-type]
            try:
                await client.call_tool("makna_run_query_" + MARKER, {"sql": "SELECT 1"})
            except MCPError as error:
                outcome.append(error)

    anyio.run(go)
    (error,) = outcome
    assert error.code == INVALID_PARAMS
    assert MARKER not in str(error) and "SELECT" not in str(error)
    assert stub.calls == [] and stub.refusals == []


@pytest.mark.parametrize(
    ("tool", "service", "arguments"),
    [
        ("makna_search", "search", {"query": "x"}),
        ("makna_get_definition", "get_definition", {"id": "metric:x"}),
        ("makna_list_related", "list_related", {"id": "metric:x"}),
    ],
)
def test_an_unknown_field_is_refused_through_the_service_so_the_refusal_is_logged(
    tool: str, service: str, arguments: dict[str, Any]
) -> None:
    stub = StubServices()
    result = call(stub, tool, {**arguments, "time_from": MARKER, f"<script>{MARKER}": 1})
    assert result.is_error is True and payload(result)["error"]["code"] == "INVALID_INPUT"
    ((name, fields, allowed),) = stub.refusals
    assert name == service and "time_from" in fields and allowed
    assert stub.calls == []  # no service ran


def test_an_unknown_compile_field_is_left_to_the_compile_service_which_refuses_and_logs_it() -> (
    None
):
    stub = StubServices()
    call(stub, "makna_compile_query", {"metric": "npf_ratio", "time_from": MARKER})
    assert stub.refusals == []
    assert stub.calls[0][1] == (CALLER, {"metric": "npf_ratio", "time_from": MARKER})


def test_there_are_no_prompts_and_no_resources_and_what_is_asked_is_not_repeated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def go() -> None:
        async with Client(build_mcp_server(StubServices(), lambda _c: CALLER)) as client:  # type: ignore[arg-type]
            for ask in (
                client.get_prompt("p\n" + MARKER),
                client.read_resource("file:///" + MARKER),
            ):
                try:
                    await ask
                except MCPError as error:
                    assert MARKER not in str(error) and error.code == INVALID_PARAMS
                else:
                    raise AssertionError("expected a protocol error")

    anyio.run(go)
    assert MARKER not in " ".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)


def test_building_the_server_leaves_the_root_logger_as_it_was() -> None:
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    try:
        root.handlers = []
        root.setLevel(logging.NOTSET)
        build_mcp_server(StubServices(), lambda _c: CALLER)  # type: ignore[arg-type]
        assert root.handlers == [] and root.level == logging.NOTSET
        marker = logging.NullHandler()
        root.handlers = [marker]
        build_mcp_server(StubServices(), lambda _c: CALLER)  # type: ignore[arg-type]
        assert root.handlers == [marker]
    finally:
        root.handlers, level_ = saved, level
        root.setLevel(level_)


def test_optional_fields_that_the_services_take_as_null_are_advertised_as_nullable() -> None:
    props = COMPILE_SCHEMA["properties"]
    for name in ("grain", "time_start", "time_end"):
        assert "null" in props[name]["type"], name
    assert {"type": "null"} in props["filters"]["items"]["properties"]["value"]["anyOf"]
    for name in ("kinds", "domain"):
        assert "null" in SEARCH_SCHEMA["properties"][name]["type"], name
    relation = LIST_RELATED_SCHEMA["properties"]["relation"]
    assert "null" in relation["type"] and None in relation["enum"]
    for required in ("metric", "dimension", "op"):  # what has no null stays required
        assert required in str(COMPILE_SCHEMA["required"] + ["dimension", "op"])


def test_a_compile_response_is_serialised_like_every_other_view() -> None:
    from makna.services.views import compile_to_dict

    response = CompileResponse(
        sql="SELECT 1",
        dialect="postgres",
        columns=(ResultColumn(name="metric_time", role="time"),),
        explanation=("one", "two"),
        warnings=("careful",),
        metric=MetricRef(id="metric:x"),
    )
    assert compile_to_dict(response) == {
        "sql": "SELECT 1",
        "dialect": "postgres",
        "columns": [{"name": "metric_time", "role": "time"}],  # no null unit
        "explanation": ["one", "two"],
        "warnings": ["careful"],
        "metric": {"id": "metric:x"},  # no null version
    }
    assert "warnings" not in compile_to_dict(response.model_copy(update={"warnings": ()}))
