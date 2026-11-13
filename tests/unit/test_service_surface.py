"""WP-12 criterion 5 (D-21) and the shape of what the services return.

- No public service function accepts SQL text: no parameter is called `sql` or `query_sql`.
  (The import check of `test_engine_not_called_by_interfaces.py` keeps `makna.services` from
  importing `makna.engine`, and is not changed.)
- The service errors have the shape of the compiler's `QueryError`.
- The views leave out empty fields (ARCHITECTURE section 12).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from typing import Any

import pytest

import makna.services as services_package
from makna.model.request import ErrorCode, QueryError
from makna.services import ServiceError, ServiceErrorCode, Services
from makna.services.errors import invalid_input
from makna.services.views import (
    DefinitionView,
    DimensionView,
    RelatedItem,
    RelatedOutcome,
    SearchHit,
    SearchOutcome,
    compact,
    cut,
)

FORBIDDEN = {"sql", "query_sql", "sql_text", "statement"}


def public_callables() -> Iterator[tuple[str, Any]]:
    for info in pkgutil.walk_packages(services_package.__path__, "makna.services."):
        module = importlib.import_module(info.name)
        for name, member in vars(module).items():
            if name.startswith("_") or getattr(member, "__module__", None) != module.__name__:
                continue
            if inspect.isfunction(member):
                yield f"{module.__name__}.{name}", member
            elif inspect.isclass(member):
                yield f"{module.__name__}.{name}", member
                for method_name, method in vars(member).items():
                    if not method_name.startswith("_") and inspect.isfunction(method):
                        yield f"{module.__name__}.{name}.{method_name}", method


def test_no_public_service_function_has_a_sql_parameter() -> None:
    seen = 0
    for name, member in public_callables():
        target = member.__init__ if inspect.isclass(member) else member
        try:
            parameters = set(inspect.signature(target).parameters)
        except (TypeError, ValueError):
            continue
        seen += 1
        assert not parameters & FORBIDDEN, f"{name} takes {parameters & FORBIDDEN}"
    assert seen > 40  # the walk really covered the package


def test_the_four_services_are_the_public_surface_of_services() -> None:
    methods = {
        name
        for name, member in vars(Services).items()
        if not name.startswith("_") and callable(member)
    }
    assert {"search", "get_definition", "list_related", "compile_query"} <= methods
    assert methods <= {"search", "get_definition", "list_related", "compile_query", "dispose"}


def test_every_service_takes_a_caller_first() -> None:
    for name in ("search", "get_definition", "list_related", "compile_query"):
        parameters = list(inspect.signature(getattr(Services, name)).parameters)
        assert parameters[:2] == ["self", "caller"], name


def test_service_errors_have_the_shape_of_query_errors() -> None:
    query = QueryError(ErrorCode.UNKNOWN_METRIC, "m", {"a": 1}).to_dict()
    service = ServiceError(ServiceErrorCode.NOT_FOUND, "m", {"a": 1}).to_dict()
    assert set(query["error"]) == set(service["error"]) == {"code", "message", "details"}
    assert service["error"]["code"] == "NOT_FOUND"
    assert ServiceError(ServiceErrorCode.INTERNAL, "m").details == {}


def test_the_service_codes_are_the_five_of_the_design() -> None:
    assert {c.value for c in ServiceErrorCode} == {
        "NOT_FOUND",
        "PERMISSION_DENIED",
        "INVALID_INPUT",
        "CATALOG_NOT_SYNCED",
        "INTERNAL",
    }
    assert not {c.value for c in ServiceErrorCode} & {c.value for c in ErrorCode}


def test_invalid_input_names_the_field_and_not_the_value() -> None:
    error = invalid_input("limit", "a whole number from 1 to 10", maximum=10)
    assert error.code == ServiceErrorCode.INVALID_INPUT
    assert error.details == {"field": "limit", "maximum": 10}


# --- views ---


def test_compact_drops_none_and_empty_but_keeps_false_and_zero() -> None:
    assert compact({"a": None, "b": [], "c": {}, "d": "", "e": False, "f": 0, "g": [None, 1]}) == {
        "e": False,
        "f": 0,
        "g": [1],
    }


def test_a_view_to_dict_has_no_empty_fields() -> None:
    hit = SearchHit(id="metric:x", label="X", kind="metric", description="d")
    assert hit.to_dict() == {"id": "metric:x", "label": "X", "kind": "metric", "description": "d"}
    dimension = DimensionView(ref="a.b", type="time", filterable=False, masked="year only")
    assert dimension.to_dict() == {
        "ref": "a.b",
        "type": "time",
        "masked": "year only",
        "filterable": False,
    }


def test_an_empty_search_and_an_empty_related_list_are_still_answers() -> None:
    assert SearchOutcome(confidence="low", guidance="g").to_dict() == {
        "confidence": "low",
        "guidance": "g",
        "hits": [],
    }
    assert RelatedOutcome().to_dict() == {"related": []}
    item = RelatedItem(id="a:b", label="B", kind="a", relation="input_of", description="d")
    assert RelatedOutcome(related=[item]).to_dict()["related"][0]["relation"] == "input_of"


def test_definition_view_keeps_only_what_a_kind_has() -> None:
    view = DefinitionView(
        id="glossary_term:npf", kind="glossary_term", label="NPF", description="d"
    )
    assert set(view.to_dict()) == {"id", "kind", "label", "description"}


def test_cut_keeps_descriptions_to_200_characters_on_one_line() -> None:
    assert cut("a  b\n c") == "a b c"
    assert len(cut("word " * 100)) <= 200
    assert cut("x" * 300).endswith("…") and len(cut("x" * 300)) == 200
    assert cut(None) == ""


@pytest.mark.parametrize("text", ["short", "x" * 200])
def test_cut_leaves_short_text_alone(text: str) -> None:
    assert cut(text) == text
