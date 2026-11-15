"""The MCP server: four read-only tools over the service layer (ARCHITECTURE section 12).

`build_mcp_server(services, caller_of)` returns an `MCPServer` with exactly four tools and
nothing else registered (no resource, no prompt, no run tool: Makna compiles and never runs,
D-21). The tools hold no logic of their own: each one passes its arguments to a service, which
checks them, applies the permission matrix of section 11, and writes the `tool_call_log` row.

Why this module overrides `list_tools` and `call_tool` instead of using the `@tool` decorator:
the decorator builds its input model from a Python signature, ignores unknown fields, and puts
pydantic's text (which repeats the rejected value) into the error. A misspelt `time_end` that is
silently dropped would give an agent a number for the wrong period, and the value of a wrong
argument can be anything the agent typed, so neither is acceptable here. The input schemas below
are written out, say `additionalProperties: false`, and are checked by the services.

**Who is calling** is not decided here. `build_mcp_server` takes a function that reads the
`Caller` out of the request; `makna.mcp.http` supplies the one that reads what its bearer-token
middleware stored in the ASGI scope of that request. There is no default and no fallback: a call
without a caller is `INTERNAL`.

**What leaves the server.** A tool result is one JSON text (`to_dict()` of the service's view).
A refusal is `isError: true` with `{"error": {"code", "message", "details"}}` from the error's
own `to_dict()`. Any other exception is `INTERNAL` with the services' fixed message: no
exception text, class name, or traceback is returned, and only the class name is logged, because
the text of a database or engine error can carry a value the caller typed or a personal value.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from mcp_types import Tool as MCPTool

from makna.mcp import texts
from makna.model.request import DEFAULT_LIMIT as COMPILE_DEFAULT_LIMIT
from makna.model.request import FILTER_OPS, MAX_LIMIT, QueryError
from makna.services import Caller, ServiceError, ServiceErrorCode, Services
from makna.services.objects import MAX_ID_LENGTH
from makna.services.related import RELATIONS
from makna.services.service import (
    DEFAULT_LIMIT,
    DETAILS,
    INTERNAL_MESSAGE,
    MAX_QUERY_LENGTH,
    SEARCH_KINDS,
)
from makna.services.service import MAX_LIMIT as MAX_SEARCH_LIMIT
from makna.services.text_check import MAX_DIMENSIONS, MAX_FILTERS, MAX_IN_VALUES, MAX_STRING
from makna.services.views import compile_to_dict

logger = logging.getLogger(__name__)

SERVER_NAME = "makna"

CallerOf = Callable[[Context | None], Caller]
"""Finds the `Caller` of the request a tool call arrived with. It raises when there is none."""

_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
"""Every tool reads. `idempotentHint` is true because repeating a call changes nothing but the
call log, which is not Makna's data about the world; `openWorldHint` is false because the tools
touch only Makna's own database."""


# --- the input schemas (JSON Schema, written out; the services check every value) ---

_STRING_LIST = {"type": "array", "items": {"type": "string", "maxLength": MAX_STRING}}

SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_LENGTH},
        "kinds": {
            "type": "array",
            "items": {"type": "string", "enum": list(SEARCH_KINDS)},
            "minItems": 1,
        },
        "domain": {"type": "string"},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_SEARCH_LIMIT,
            "default": DEFAULT_LIMIT,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

GET_DEFINITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "maxLength": MAX_ID_LENGTH},
        "detail": {"type": "string", "enum": list(DETAILS), "default": "full"},
    },
    "required": ["id"],
    "additionalProperties": False,
}

LIST_RELATED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "maxLength": MAX_ID_LENGTH},
        "relation": {"type": "string", "enum": list(RELATIONS)},
    },
    "required": ["id"],
    "additionalProperties": False,
}

_SCALAR = {"type": ["string", "number", "boolean"]}

COMPILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "maxLength": MAX_STRING},
        "dimensions": {**_STRING_LIST, "maxItems": MAX_DIMENSIONS},
        "filters": {
            "type": "array",
            "maxItems": MAX_FILTERS,
            "items": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string", "maxLength": MAX_STRING},
                    "op": {"type": "string", "enum": list(FILTER_OPS)},
                    "value": {
                        "anyOf": [
                            _SCALAR,
                            {"type": "array", "items": _SCALAR, "maxItems": MAX_IN_VALUES},
                        ]
                    },
                },
                "required": ["dimension", "op"],
                "additionalProperties": False,
            },
        },
        "grain": {"type": "string", "maxLength": MAX_STRING},
        "time_start": {"type": "string", "maxLength": MAX_STRING},
        "time_end": {"type": "string", "maxLength": MAX_STRING},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "default": COMPILE_DEFAULT_LIMIT,
        },
    },
    "required": ["metric"],
    "additionalProperties": False,
}


# --- the tools ---


def _search(services: Services, caller: Caller, args: Mapping[str, Any]) -> dict[str, Any]:
    return services.search(
        caller,
        args.get("query"),  # type: ignore[arg-type]  # the service checks the type
        kinds=args.get("kinds"),
        domain=args.get("domain"),
        limit=args.get("limit", DEFAULT_LIMIT),
    ).to_dict()


def _get_definition(services: Services, caller: Caller, args: Mapping[str, Any]) -> dict[str, Any]:
    return services.get_definition(
        caller,
        args.get("id"),  # type: ignore[arg-type]
        detail=args.get("detail", "full"),
    ).to_dict()


def _list_related(services: Services, caller: Caller, args: Mapping[str, Any]) -> dict[str, Any]:
    return services.list_related(
        caller,
        args.get("id"),  # type: ignore[arg-type]
        relation=args.get("relation"),
    ).to_dict()


def _compile_query(services: Services, caller: Caller, args: Mapping[str, Any]) -> dict[str, Any]:
    return compile_to_dict(services.compile_query(caller, dict(args)))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[Services, Caller, Mapping[str, Any]], dict[str, Any]]

    @property
    def fields(self) -> frozenset[str]:
        return frozenset(self.schema["properties"])


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("makna_search", texts.SEARCH_DESCRIPTION, SEARCH_SCHEMA, _search),
    ToolSpec(
        "makna_get_definition",
        texts.GET_DEFINITION_DESCRIPTION,
        GET_DEFINITION_SCHEMA,
        _get_definition,
    ),
    ToolSpec(
        "makna_list_related", texts.LIST_RELATED_DESCRIPTION, LIST_RELATED_SCHEMA, _list_related
    ),
    ToolSpec(
        "makna_compile_query", texts.COMPILE_QUERY_DESCRIPTION, COMPILE_SCHEMA, _compile_query
    ),
)
_BY_NAME = {tool.name: tool for tool in TOOLS}


# --- results and errors ---


def _dump(payload: Mapping[str, Any]) -> str:
    """Compact JSON, readable text (Indonesian stays as typed), and never `NaN`."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _internal() -> dict[str, Any]:
    return ServiceError(ServiceErrorCode.INTERNAL, INTERNAL_MESSAGE).to_dict()


def _result(payload: Mapping[str, Any], *, is_error: bool) -> CallToolResult:
    try:
        text = _dump(payload)
    except (TypeError, ValueError) as error:
        # A detail the JSON encoder cannot take. Only the class is logged.
        logger.error("tool result could not be serialised: error=%s", type(error).__name__)
        text, is_error = _dump(_internal()), True
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)


def _error(error: ServiceError | QueryError) -> CallToolResult:
    return _result(error.to_dict(), is_error=True)


def _unknown_tool() -> CallToolResult:
    return _error(
        ServiceError(
            ServiceErrorCode.INVALID_INPUT,
            "There is no tool with that name. Use one of: "
            + ", ".join(tool.name for tool in TOOLS)
            + ".",
            {"field": "name", "allowed": [tool.name for tool in TOOLS]},
        )
    )


def _unknown_fields(tool: ToolSpec, arguments: Mapping[str, Any]) -> CallToolResult | None:
    """A refusal for an argument the tool does not have, or `None`. A misspelt field must not be
    dropped silently: the agent would believe a filter or a period was applied. Only names that
    look like names are repeated back; the values never are."""
    unknown = [key for key in arguments if key not in tool.fields]
    if not unknown:
        return None
    shown = sorted({k for k in unknown if isinstance(k, str) and _FIELD_NAME.fullmatch(k)})[:10]
    allowed = sorted(tool.fields)
    return _error(
        ServiceError(
            ServiceErrorCode.INVALID_INPUT,
            f"{tool.name} has no field {', '.join(shown) if shown else 'with that name'}. "
            f"Use only: {', '.join(allowed)}.",
            {"fields": shown, "allowed": allowed},
        )
    )


class MaknaServer(MCPServer[Any]):
    """`MCPServer` with the four Makna tools. See the module docstring."""

    def __init__(self, services: Services, caller_of: CallerOf) -> None:
        # `log_level` WARNING keeps the root logger where it was: the SDK calls
        # `logging.basicConfig` with this level when the server is built.
        super().__init__(SERVER_NAME, instructions=texts.INSTRUCTIONS, log_level="WARNING")
        self._services = services
        self._caller_of = caller_of

    async def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.schema,
                annotations=ANNOTATIONS,
            )
            for tool in TOOLS
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult:
        tool = _BY_NAME.get(name)
        if tool is None:
            return _unknown_tool()
        refused = _unknown_fields(tool, arguments)
        if refused is not None:
            return refused
        try:
            caller = self._caller_of(context)
            # The services are synchronous and read the database: off the event loop.
            payload = await anyio.to_thread.run_sync(
                partial(tool.run, self._services, caller, dict(arguments))
            )
        except (ServiceError, QueryError) as error:
            return _error(error)
        except Exception as error:
            logger.error("tool call failed: tool=%s error=%s", tool.name, type(error).__name__)
            return _result(_internal(), is_error=True)
        return _result(payload, is_error=False)


def build_mcp_server(services: Services, caller_of: CallerOf) -> MaknaServer:
    """The server for `services`. `caller_of` says who is calling (see the module docstring)."""
    return MaknaServer(services, caller_of)
