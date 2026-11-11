"""WP-08: request, response, and error types of ARCHITECTURE sections 7.1, 7.2, 7.7."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from makna.model.definitions import RequestBody
from makna.model.request import (
    DEFAULT_LIMIT,
    CompileResponse,
    ErrorCode,
    MetricRef,
    QueryError,
    QueryRequest,
    ResultColumn,
)

ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md"


def test_a_request_has_the_defaults_of_section_7_1() -> None:
    request = QueryRequest(metric="npf_ratio")
    assert request.dimensions == ()
    assert request.filters == ()
    assert request.grain is None
    assert request.time_start is None and request.time_end is None
    assert request.limit == DEFAULT_LIMIT == 1000


def test_a_request_is_read_from_json_shaped_data_and_is_comparable() -> None:
    data = {
        "metric": "collection_rate",
        "dimensions": ["branch.region_name"],
        "filters": [{"dimension": "product.product_category", "op": "in", "value": ["car_backed"]}],
        "grain": "month",
        "time_start": "2026-01-01",
        "time_end": "2026-07-01",
        "limit": 50,
    }
    request = QueryRequest.model_validate(data)
    assert request == QueryRequest.model_validate(data)
    assert request.filters[0].value == ("car_backed",)
    assert request.dimensions == ("branch.region_name",)


def test_the_request_leaves_judging_values_to_the_planner() -> None:
    odd = QueryRequest(
        metric="m", grain="fortnight", time_start="soon", limit=10**6,
        filters=({"dimension": "a.b", "op": "like", "value": "x"},),
    )  # fmt: skip
    assert odd.grain == "fortnight" and odd.limit == 10**6


def test_an_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"metric": "m", "dimension": ["a.b"]})


def test_an_example_request_becomes_a_request_for_its_metric() -> None:
    body = RequestBody.model_validate(
        {
            "dimensions": ["branch.region_name"],
            "filters": [{"dimension": "branch.province", "op": "in", "value": ["a", "b"]}],
            "grain": "quarter",
            "time_start": "2025-10-01",
            "time_end": "2026-07-01",
        }
    )
    request = QueryRequest.from_example("npf_ratio", body)
    assert request.metric == "npf_ratio"
    assert request.filters[0].value == ("a", "b")
    assert request.limit == DEFAULT_LIMIT
    assert request.grain == "quarter"


def test_every_error_code_of_section_7_7_exists() -> None:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    table = text.split("### 7.7 Errors")[1].split("### 7.8")[0]
    documented = set(re.findall(r"`([A-Z_]{5,})`", table.split("|---|---|---|\n")[1]))
    assert documented == {str(code) for code in ErrorCode}


def test_an_error_serialises_as_a_tool_error() -> None:
    error = QueryError(ErrorCode.INVALID_GRAIN, "Use month.", {"allowed_grains": ["month"]})
    assert error.code == "INVALID_GRAIN"
    assert error.to_dict() == {
        "error": {
            "code": "INVALID_GRAIN",
            "message": "Use month.",
            "details": {"allowed_grains": ["month"]},
        }
    }
    assert "INVALID_GRAIN" in str(error)


def test_a_response_carries_what_makna_compile_query_returns() -> None:
    response = CompileResponse(
        sql="SELECT 1",
        dialect="postgres",
        columns=(ResultColumn(name="npf_ratio", role="metric", unit="percent"),),
        metric=MetricRef(id="metric:npf_ratio", version=3),
    )
    assert response.explanation == () and response.warnings == ()
    assert response.model_dump()["metric"] == {"id": "metric:npf_ratio", "version": 3}
