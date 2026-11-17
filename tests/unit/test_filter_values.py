"""WP-15C (C-9): filter values that pass the planner and fail on the engine.

A time value is `YYYY-MM-DD` or `YYYY-MM-DD HH:MM[:SS]` and nothing else; a boolean `limit` is not
a number; and the service refuses a value that cannot be compared with the column a dimension
reads, because the catalog types that column (the planner cannot know it).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from makna.compiler.compile import compile_request
from makna.compiler.plan import plan_request
from makna.model.request import ErrorCode, QueryError, QueryRequest
from makna.model.values import is_date_only, is_time_value, value_fits_column
from makna.services.filter_types import check_filter_types

from .compiler_support import lending, request

ACCEPTED_TIME = ["2026-01-01", "2026-01-01 10:00", "2026-01-01 10:00:59", "2024-02-29"]
REFUSED_TIME = [
    "2026-W01-1",  # an ISO week date: Python reads it, PostgreSQL does not
    "2026-W01",
    "2026-001",  # an ordinal date
    "20260101",
    "2026-01-01T10",
    "2026-01-01T10:00:00",
    "2026-01-01 10",
    "2026-01-01 10:00:00.5",
    "2026-01-01 10:00:00+07:00",
    "2026-01-01 10:00:00Z",
    "2026-02-30",
    "2026-13-01",
    "2023-02-29",
    " 2026-01-01",
    "2026-01-01 ",
    "２０２６-０１-０１",  # full-width digits
    "٢٠٢٦-٠١-٠١",  # Arabic-Indic digits
    "last week",
    "",
]


@pytest.mark.parametrize("value", ACCEPTED_TIME)
def test_a_time_value_is_a_date_or_a_date_and_a_time(value: str) -> None:
    assert is_time_value(value)


@pytest.mark.parametrize("value", REFUSED_TIME)
def test_every_other_spelling_of_a_time_value_is_refused(value: str) -> None:
    assert not is_time_value(value)


def test_only_a_ten_character_date_is_a_date_only_value() -> None:
    assert is_date_only("2026-01-01")
    assert not is_date_only("2026-01-01 10:00")
    assert not is_date_only("2026-W01-1")


def disbursed(value: Any, op: str = ">=") -> QueryRequest:
    return request(
        "disbursement_amount",
        filters=[{"dimension": "contract.disbursement_date", "op": op, "value": value}],
    )


@pytest.mark.parametrize("value", REFUSED_TIME)
def test_the_planner_refuses_a_time_filter_value_postgresql_cannot_read(value: str) -> None:
    with pytest.raises(QueryError) as caught:
        plan_request(lending(), disbursed(value))
    assert caught.value.code == ErrorCode.INVALID_FILTER


def test_the_planner_refuses_such_a_value_inside_an_in_list() -> None:
    with pytest.raises(QueryError) as caught:
        plan_request(lending(), disbursed(["2026-01-01", "2026-W01-1"], "in"))
    assert caught.value.code == ErrorCode.INVALID_FILTER


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("2026-01-01", "DATE"),
        ("2026-01-01 10:00", "TIMESTAMP"),
        ("2026-01-01 10:00:59", "TIMESTAMP"),
    ],
)
def test_the_two_spellings_compile_to_a_typed_constant(value: str, kind: str) -> None:
    sql = compile_request(lending(), disbursed(value)).sql
    assert f"CAST('{value}' AS {kind})" in sql


# --- limit -----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_a_boolean_limit_is_refused(value: bool) -> None:
    with pytest.raises(ValidationError, match="limit"):
        QueryRequest.model_validate({"metric": "disbursement_amount", "limit": value})


def test_a_whole_number_limit_is_still_accepted() -> None:
    assert QueryRequest.model_validate({"metric": "disbursement_amount", "limit": 1}).limit == 1
    assert QueryRequest.model_validate({"metric": "disbursement_amount"}).limit == 1000


# --- the catalog type ------------------------------------------------------------------------

FITS = [
    # (dimension type, value, catalog type, fits)
    ("categorical", "Java", "string", True),
    ("categorical", 5, "string", False),
    ("categorical", 5.5, "string", False),
    ("categorical", "5", "integer", True),
    ("categorical", "5.5", "integer", False),
    ("categorical", "abc", "integer", False),
    ("categorical", 5, "integer", True),
    ("categorical", 5.5, "integer", True),
    ("categorical", "5.5", "decimal", True),
    ("categorical", "1e3", "float", True),
    ("categorical", "abc", "decimal", False),
    ("numeric", 12, "decimal", True),
    ("numeric", 12, "string", False),
    ("numeric", 12, "boolean", False),
    ("numeric", 12, "date", False),
    ("boolean", True, "boolean", True),
    ("boolean", True, "integer", False),
    ("boolean", False, "string", False),
    ("categorical", "true", "boolean", True),
    ("categorical", "maybe", "boolean", False),
    ("time", "2026-01-01", "date", True),
    ("time", "2026-01-01 10:00", "timestamp", True),
    ("time", "2026-01-01", "string", False),
    ("time", "2026-01-01", "integer", False),
    ("categorical", "2026-01-01", "date", True),
    ("categorical", "soon", "date", False),
    ("categorical", 5, "date", False),
    ("categorical", "anything", "other", True),
    ("numeric", 5, "other", True),
    ("categorical", "x", "a type the catalog does not have", True),
]


@pytest.mark.parametrize(("dimension", "value", "data_type", "fits"), FITS)
def test_value_fits_column(dimension: str, value: Any, data_type: str, fits: bool) -> None:
    assert value_fits_column(dimension, value, data_type) is fits


def _types(**overrides: str) -> dict[tuple[str, str], str]:
    found = {
        ("lending.core.customers", "province"): "string",
        ("lending.core.contracts", "tenor_months"): "integer",
        ("lending.core.contracts", "disbursement_date"): "date",
    }
    found.update({tuple(k.split(".")): v for k, v in overrides.items()})  # type: ignore[misc]
    return found


def planned(**fields: Any) -> Any:
    return plan_request(lending(), request("new_customer_count", **fields))


def test_a_number_against_a_text_column_is_an_invalid_filter() -> None:
    plan = planned(filters=[{"dimension": "customer.province", "op": "=", "value": 5}])
    with pytest.raises(QueryError) as caught:
        check_filter_types(lending(), plan, _types())
    error = caught.value
    assert error.code == ErrorCode.INVALID_FILTER
    assert error.details["catalog_type"] == "string"
    assert error.details["dimension"] == "customer.province"
    assert "value 5" in error.message and "text" in error.message


def test_a_matching_value_passes_and_an_unknown_column_type_is_not_checked() -> None:
    plan = planned(filters=[{"dimension": "customer.province", "op": "=", "value": "Jawa Barat"}])
    check_filter_types(lending(), plan, _types())
    plan = planned(filters=[{"dimension": "customer.province", "op": "=", "value": 5}])
    check_filter_types(lending(), plan, {})  # a column the catalog does not list: nothing to say


def test_every_value_of_an_in_list_is_checked() -> None:
    plan = planned(filters=[{"dimension": "customer.province", "op": "in", "value": ["Jawa", 7]}])
    with pytest.raises(QueryError) as caught:
        check_filter_types(lending(), plan, _types())
    assert "value 7" in caught.value.message


def test_a_filter_without_a_value_is_not_checked() -> None:
    plan = planned(filters=[{"dimension": "customer.province", "op": "is_null"}])
    check_filter_types(lending(), plan, _types())


def _shop_with_dimensions() -> Any:
    from .compiler_support import entity_doc, shop
    from .definition_fixtures import Files, _dim

    def add(files: Files) -> None:
        entity_doc(files, "order")["dimensions"] += [
            _dim("shouted_status", "categorical", expr="UPPER(status)"),
            _dim("status_again", "categorical", expr="status"),
        ]

    return shop(add)


def test_a_dimension_that_reads_an_expression_is_not_checked() -> None:
    """`UPPER(status)` is text whatever the column is: not a column's type, so no error here."""
    model = _shop_with_dimensions()
    plan = plan_request(
        model,
        request("revenue", filters=[{"dimension": "order.shouted_status", "op": "=", "value": 5}]),
    )
    check_filter_types(model, plan, {("shop.core.orders", "status"): "string"})


def test_a_dimension_that_is_just_a_column_is_checked_by_that_column() -> None:
    model = _shop_with_dimensions()
    plan = plan_request(
        model,
        request("revenue", filters=[{"dimension": "order.status_again", "op": "=", "value": 5}]),
    )
    with pytest.raises(QueryError) as caught:
        check_filter_types(model, plan, {("shop.core.orders", "status"): "string"})
    assert caught.value.code == ErrorCode.INVALID_FILTER


def test_the_value_of_a_personal_dimension_is_not_quoted() -> None:
    from makna.compiler.snapshot import Model
    from makna.model.governance import GovernancePolicy

    model = Model.build(
        list(lending().entities.values()),
        list(lending().metrics.values()),
        derived_sensitivity={("lending.core.customers", "province"): "personal"},
    )
    plan = plan_request(
        model,
        request(
            "new_customer_count",
            filters=[{"dimension": "customer.province", "op": "=", "value": 5}],
        ),
        governance=GovernancePolicy.custom(allow_personal_filters=True),
    )
    with pytest.raises(QueryError) as caught:
        check_filter_types(model, plan, _types())
    assert "value 5" not in caught.value.message and "the value" in caught.value.message
    assert caught.value.details["allowed_values"] == []
