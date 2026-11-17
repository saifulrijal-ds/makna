"""WP-15C (C-9): the service refuses a filter value that cannot match the catalog type of the
column, and a boolean limit, before any SQL is written.

The planner plans against the definitions and cannot know that `customer.province` is text; the
synced catalog says so, and compiling needs a synced catalog (D-30).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine, text

from makna.model.request import QueryError
from makna.services import ServiceError
from makna.services.errors import ServiceErrorCode

from . import service_support as sup
from .service_support import Users


def compile_filter(world: tuple[Engine, Users], dimension: str, op: str, value: Any) -> Any:
    engine, users = world
    return sup.services(engine).compile_query(
        users.caller("viewer"),
        {
            "metric": "new_customer_count",
            "filters": [{"dimension": dimension, "op": op, "value": value}],
        },
    )


def test_a_number_against_a_text_dimension_is_an_invalid_filter_and_a_text_value_compiles(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    before = sup.log_count(engine)
    with pytest.raises(QueryError) as caught:
        compile_filter(service_world, "customer.province", "=", 5)
    error = caught.value
    assert error.code == "INVALID_FILTER"
    assert error.details["catalog_type"] == "string"
    assert "text" in error.message
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, error_code FROM tool_call_log ORDER BY id OFFSET :n"),
            {"n": before},
        ).one()
    assert tuple(row) == ("refused", "INVALID_FILTER")
    ok = compile_filter(service_world, "customer.province", "=", "Jawa Barat")
    assert "'Jawa Barat'" in ok.sql


def test_a_number_inside_a_list_of_text_values_is_refused_too(
    service_world: tuple[Engine, Users],
) -> None:
    with pytest.raises(QueryError) as caught:
        compile_filter(service_world, "customer.province", "in", ["Jawa Barat", 3])
    assert caught.value.code == "INVALID_FILTER"


def test_a_time_value_postgresql_cannot_read_is_an_invalid_filter(
    service_world: tuple[Engine, Users],
) -> None:
    for bad in ("2026-W01-1", "2026-01-01T10"):
        with pytest.raises(QueryError) as caught:
            compile_filter(service_world, "customer.registered_date", ">=", bad)
        assert caught.value.code == "INVALID_FILTER", bad
    assert compile_filter(service_world, "customer.registered_date", ">=", "2026-01-01 10:00").sql


def test_a_boolean_limit_is_invalid_input(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    with pytest.raises(ServiceError) as caught:
        sup.services(engine).compile_query(
            users.caller("viewer"), {"metric": "new_customer_count", "limit": True}
        )
    assert caught.value.code == ServiceErrorCode.INVALID_INPUT
    assert caught.value.details["fields"] == ["limit"]
