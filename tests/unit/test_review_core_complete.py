"""Milestone review "core complete": tests that prove findings of docs/reviews/core-complete.md.

Each test states what should hold and fails today. They are marked `xfail(strict=True)` so that
the suite stays green while the finding is open: remove the marker to see the failure, and when
the finding is fixed the test passes, the strict marker turns that into a failure, and the marker
must go. The finding number is the number in the review report.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from makna.compiler.compile import compile_request
from makna.compiler.snapshot import Model
from makna.definitions.loader import load_definitions
from makna.definitions.validate import validate
from makna.model.definitions import Entity, Metric, parse_definition
from makna.model.request import ErrorCode, QueryError

from . import definition_fixtures as fx
from .compiler_support import lending, request


def _shop_with_revenue_expr(expr: str, tmp_path: Path) -> tuple[list[str], Model]:
    """The `shop` fixture with the expression of the `revenue` measure replaced: the codes that
    validation reports for it, and the model the compiler would plan against."""
    files = copy.deepcopy(fx.base())
    measures: list[dict[str, Any]] = files["shop/entities/order.yaml"]["measures"]
    next(m for m in measures if m["name"] == "revenue")["expr"] = expr
    codes = [issue.code for issue in validate(load_definitions(fx.write(files, tmp_path)))]
    docs = [parse_definition(doc) for doc in files.values()]
    model = Model.build(
        [d for d in docs if isinstance(d, Entity)], [d for d in docs if isinstance(d, Metric)]
    )
    return codes, model


@pytest.mark.xfail(strict=True, reason="review finding 5: expressions are not read as PostgreSQL")
def test_a_power_operator_is_refused_or_stays_a_power(tmp_path: Path) -> None:
    """In PostgreSQL `x ^ 2` is x squared. A definition that says so must either be refused by
    validation or compile to a power. Today it passes validation and compiles to `x # 2`, which
    PostgreSQL reads as a bitwise exclusive or: a wrong number with no error."""
    codes, model = _shop_with_revenue_expr("total_amount ^ 2", tmp_path)
    if "V05" in codes:
        return
    sql = compile_request(model, request("revenue"), "postgres").sql
    assert "#" not in sql, sql


@pytest.mark.xfail(strict=True, reason="review finding 5: expressions are not read as PostgreSQL")
def test_an_int8_cast_is_refused_or_stays_a_big_integer(tmp_path: Path) -> None:
    """`int8` is PostgreSQL's eight-byte integer. Today the cast is printed as `SMALLINT`, which
    overflows above 32,767 (and the engine's overflow error text quotes the value)."""
    codes, model = _shop_with_revenue_expr("CAST(total_amount AS int8)", tmp_path)
    if "V05" in codes:
        return
    sql = compile_request(model, request("revenue"), "postgres").sql
    assert "SMALLINT" not in sql, sql


@pytest.mark.xfail(strict=True, reason="review finding 6: a join to a non-key is not checked")
def test_a_relationship_must_point_at_the_primary_key_of_its_target(tmp_path: Path) -> None:
    """ARCHITECTURE 7.3 says a join along a relationship can never multiply rows. That holds only
    if the target columns are the target's key. Here `order.customer` joins on `segment`, which
    many customers share, so every order would be counted once per customer of that segment.
    Validation should report it. Today it reports nothing."""
    files = copy.deepcopy(fx.base())
    relationships: list[dict[str, Any]] = files["shop/entities/order.yaml"]["relationships"]
    relationships[0]["keys"] = [{"from": "customer_segment", "to": "segment"}]
    issues = validate(load_definitions(fx.write(files, tmp_path)))
    assert issues, "a relationship whose target columns are not the target's primary key passed"


@pytest.mark.xfail(strict=True, reason="review finding 9: Python's ISO parser accepts more")
def test_a_time_filter_value_postgresql_cannot_read_is_an_invalid_filter() -> None:
    """A wrong request must fail with an actionable error, not produce SQL the engine rejects
    (ARCHITECTURE 8.4). `2026-W01-1` is an ISO week date: Python accepts it, PostgreSQL's
    `CAST('2026-W01-1' AS DATE)` does not. Today the request compiles."""
    with pytest.raises(QueryError) as caught:
        compile_request(
            lending(),
            request(
                "disbursement_amount",
                filters=[
                    {"dimension": "contract.disbursement_date", "op": ">=", "value": "2026-W01-1"}
                ],
            ),
            "postgres",
        )
    assert caught.value.code == ErrorCode.INVALID_FILTER
