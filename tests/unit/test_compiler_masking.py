"""WP-10 criterion 1: the masking methods, as expressions and in compiled requests.

The mask expressions are sqlglot nodes. Integer division differs between the engines
(docs/notes/integer-division-differs-between-postgres-and-duckdb.md), so the bucket test runs the
expression on DuckDB and also pins the printed PostgreSQL text.
"""

from __future__ import annotations

import datetime
import math
from typing import Any

import duckdb
import pytest
from sqlglot import exp

from makna.compiler import governance
from makna.compiler.plan import plan_request
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import build_tree, to_sql
from makna.model.definitions import Dimension, MaskBucket, MaskRedact, MaskTruncateDate
from makna.model.governance import GovernancePolicy

from .compiler_support import lending, request
from .duckdb_support import run_tree


def masked(model: Model, ref: str, dimension: Dimension) -> Model:
    """`model` with the dimension `ref` replaced by `dimension`."""
    entity_name, name = ref.split(".")
    entity = model.entities[entity_name]
    updated = entity.model_copy(
        update={"dimensions": [dimension if d.name == name else d for d in entity.dimensions]}
    )
    return Model.build(
        [updated, *[e for e in model.entities.values() if e.name != entity_name]],
        model.metrics.values(),
        derived_sensitivity=model.derived_sensitivity,
    )


def dimension_of(model: Model, ref: str, **update: Any) -> Dimension:
    found = model.dimension(ref)
    assert found is not None
    return found[1].model_copy(update=update)


def run(model: Model, metric: str, **fields: Any) -> tuple[list[tuple[Any, ...]], list[str]]:
    plan = plan_request(model, request(metric, **fields), governance=GovernancePolicy.strict(2))
    con = _connection()
    rows = run_tree(con, build_tree(model, plan))
    return rows, [c.name for c in plan.columns]


_CON: duckdb.DuckDBPyConnection | None = None


def _connection() -> duckdb.DuckDBPyConnection:
    assert _CON is not None
    return _CON


@pytest.fixture(autouse=True)
def _bind(lending_db: duckdb.DuckDBPyConnection) -> None:
    global _CON
    _CON = lending_db


# ---- criterion 1 ------------------------------------------------------------------------


def test_birth_year_returns_only_first_of_january_dates() -> None:
    rows, columns = run(lending(), "new_customer_count", dimensions=["customer.birth_year"])
    index = columns.index("customer__birth_year")
    years = [row[index] for row in rows]
    assert len(years) > 3
    assert all(y is None or (y.month, y.day) == (1, 1) for y in years)
    assert all(isinstance(y, datetime.date) for y in years if y is not None)


def test_a_numeric_dimension_with_a_bucket_of_ten_returns_multiples_of_ten() -> None:
    base = with_numeric_personal(lending())
    rows, columns = run(base, "new_customer_count", dimensions=["customer.score"])
    values = [row[columns.index("customer__score")] for row in rows]
    assert len(values) > 3
    assert all(v is None or float(v) % 10 == 0 for v in values)
    assert any(v is not None and float(v) > 0 for v in values)


def test_redact_returns_three_stars() -> None:
    model = masked(lending(), "customer.email", dimension_of(lending(), "customer.email",
                   masking=MaskRedact(method="redact")))  # fmt: skip
    rows, columns = run(model, "new_customer_count", dimensions=["customer.email"])
    assert [row[columns.index("customer__email")] for row in rows] == ["***"]


def with_numeric_personal(model: Model) -> Model:
    """A personal numeric dimension `customer.score` (birth year as a number) with bucket 10."""
    entity = model.entities["customer"]
    score = dimension_of(
        model,
        "customer.birth_year",
        name="score",
        type="numeric",
        expr="EXTRACT(YEAR FROM birth_date) - 1900",
        masking=MaskBucket(method="bucket", width=10),
    )
    updated = entity.model_copy(update={"dimensions": [*entity.dimensions, score]})
    others = [e for e in model.entities.values() if e.name != "customer"]
    return Model.build([updated, *others], model.metrics.values())


# ---- the expressions --------------------------------------------------------------------


def evaluate(node: exp.Expr, dialect: str = "duckdb") -> Any:
    row = duckdb.connect().execute(exp.select(node).sql(dialect=dialect)).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize(
    ("value", "width", "want"),
    [(-3, 10, -10), (-10, 10, -10), (0, 10, 0), (9, 10, 0), (10, 10, 10), (19, 10, 10),
     (27, 5, 25), (7.5, 2.5, 7.5), (2.4, 2.5, 0)],
)  # fmt: skip
def test_bucket_is_the_lower_bound_of_the_band_for_integers_and_fractions(
    value: float, width: float, want: float
) -> None:
    node = governance.mask_expression(
        MaskBucket(method="bucket", width=width), exp.Literal.number(value)
    )
    assert math.isclose(float(evaluate(node)), want)


def test_bucket_does_not_divide_integers_on_postgres() -> None:
    node = governance.mask_expression(MaskBucket(method="bucket", width=10), exp.column("n"))
    text = node.sql(dialect="postgres")
    assert "DOUBLE PRECISION" in text
    assert "FLOOR(" in text


def test_a_whole_width_prints_without_a_decimal_point() -> None:
    node = governance.mask_expression(MaskBucket(method="bucket", width=10.0), exp.column("n"))
    assert "10.0" not in node.sql(dialect="postgres")


@pytest.mark.parametrize("grain", ["month", "quarter", "year"])
def test_truncate_date_gives_the_first_day_of_the_period(grain: str) -> None:
    node = governance.mask_expression(
        MaskTruncateDate(method="truncate_date", grain=grain),  # type: ignore[arg-type]
        exp.Cast(this=exp.Literal.string("2026-08-17"), to=exp.DataType.build("date")),
    )
    first = {"month": (8, 1), "quarter": (7, 1), "year": (1, 1)}[grain]
    got = evaluate(node)
    assert (got.month, got.day) == first


def test_redact_is_a_typed_constant_so_it_can_sit_in_a_group_by_on_postgres() -> None:
    node = governance.mask_expression(MaskRedact(method="redact"), exp.column("email"))
    assert isinstance(node, exp.Cast)  # a bare string constant in GROUP BY fails on PostgreSQL
    assert node.sql(dialect="postgres") == "CAST('***' AS TEXT)"


def test_the_postgres_text_of_a_masked_request_carries_the_mask() -> None:
    plan = plan_request(
        lending(), request("new_customer_count", dimensions=["customer.birth_year"])
    )
    sql = to_sql(build_tree(lending(), plan), "postgres")
    assert "DATE_TRUNC('YEAR'" in sql.upper()


# ---- the explanation names masked dimensions and their method --------------------------


def explanation_of(model: Model, metric: str, **fields: Any) -> list[str]:
    from makna.compiler.compile import compile_request

    return list(compile_request(model, request(metric, **fields)).explanation)


def test_the_explanation_names_a_masked_dimension_and_its_method() -> None:
    sentences = explanation_of(lending(), "new_customer_count", dimensions=["customer.birth_year"])
    [sentence] = [s for s in sentences if "customer.birth_year" in s and "personal" in s]
    assert "truncate_date" in sentence
    assert "year" in sentence


def test_the_explanation_names_redact_and_bucket_too() -> None:
    redacted = masked(lending(), "customer.email", dimension_of(lending(), "customer.email",
                      masking=MaskRedact(method="redact")))  # fmt: skip
    [sentence] = [
        s
        for s in explanation_of(redacted, "new_customer_count", dimensions=["customer.email"])
        if "customer.email" in s and "personal" in s
    ]
    assert "redact" in sentence and "***" in sentence
    bucketed = with_numeric_personal(lending())
    [sentence] = [
        s
        for s in explanation_of(bucketed, "new_customer_count", dimensions=["customer.score"])
        if "customer.score" in s and "personal" in s
    ]
    assert "bucket" in sentence and "10" in sentence


def test_a_dimension_that_is_not_personal_is_not_described_as_masked() -> None:
    sentences = explanation_of(lending(), "new_customer_count", dimensions=["customer.province"])
    assert not any("personal" in s for s in sentences)
