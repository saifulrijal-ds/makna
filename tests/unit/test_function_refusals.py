"""WP-15C: functions and spellings that PostgreSQL and DuckDB read differently are refused.

The parity test of the engine tier (`tests/engine/test_function_parity.py`) finds them; these
tests keep the refusals in the unit tier, so that a change to the allow-list fails here and not
only where a PostgreSQL is available. D-32 (2): refuse what the two readings disagree on.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from makna.definitions.validate import _dialect_problem, _parse
from makna.model.functions import (
    ALLOWED_CAST_TYPES,
    TEXT_CAST_TYPES,
    first_disallowed_function,
    first_value_dependent_cast,
    node_problem,
)

D = "DATE '2026-03-15'"
TS = "TIMESTAMP '2026-03-15 10:20:30.5'"


def problem(sql: str) -> str | None:
    tree = sqlglot.parse_one(sql, into=exp.Condition)
    return first_disallowed_function(tree)


REFUSED = {
    "GREATEST(1, NULL)": "CASE",
    "GREATEST(1, 5, 3)": "CASE",
    "LEAST(4, NULL)": "CASE",
    "LEAST(4, 2, 3)": "CASE",
    "ROUND(2.5)": "half to even",
    "ROUND(CAST(2.5 AS DOUBLE PRECISION), 1)": "half to even",
    "TRY_CAST('x' AS INT)": "TRY_CAST",
    "CAST('abc' AS CHAR)": "type",
    "CAST('abc' AS CHAR(2))": "type",
    "CAST(300 AS TINYINT)": "type",
    f"CAST({TS} AS TIMESTAMPTZ)": "type",
    "CAST('abc' AS VARCHAR(2))": "length",
    "SUBSTRING('abcdef', -1, 3)": "SUBSTRING",
    "SUBSTRING('abcdef', 0, 3)": "SUBSTRING",
    "SUBSTRING('abcdef', x, 3)": "SUBSTRING",
    "SUBSTRING('abcdef', 2, y)": "SUBSTRING",
    "SUBSTRING('abcdef', 2, -1)": "SUBSTRING",
    "SUBSTRING('abcdef' FROM 0 FOR 2)": "SUBSTRING",
    "SPLIT_PART(x, '', 1)": "SPLIT_PART",
    "SPLIT_PART('abc', '', 1)": "SPLIT_PART",
    "SPLIT_PART('a,b', ',', 0)": "SPLIT_PART",
    "SPLIT_PART('a,b', ',', n)": "SPLIT_PART",
    "SPLIT_PART('a,b', d, 1)": "SPLIT_PART",
    "SPLIT_PART('a,b', ',', 1.5)": "SPLIT_PART",
    f"EXTRACT(SECOND FROM {TS})": "SECOND",
    f"EXTRACT(EPOCH FROM {TS})": "EPOCH",
    f"EXTRACT(MILLISECOND FROM {TS})": "MILLISECOND",
    f"EXTRACT(TIMEZONE FROM {TS})": "TIMEZONE",
}


@pytest.mark.parametrize("sql", sorted(REFUSED))
def test_the_allow_list_refuses(sql: str) -> None:
    found = problem(sql)
    assert found is not None and REFUSED[sql] in found, (sql, found)


def test_a_json_cast_is_refused_explicitly() -> None:
    tree = exp.JSONCast(this=exp.column("x"), to=exp.DataType.build("json"))
    assert (
        first_disallowed_function(tree)
        == "a cast to JSON, which DuckDB and PostgreSQL read differently"
    )
    assert node_problem(tree) == first_disallowed_function(tree)  # the branch's own message


ACCEPTED = [
    "SUBSTRING('abcdef', 2, 3)",
    "SUBSTRING('abcdef', 2)",
    "SUBSTRING('abcdef', 2, 0)",
    "SUBSTRING('abcdef' FROM 2 FOR 3)",
    "SPLIT_PART('a,b,c', ',', 2)",
    "SPLIT_PART('a,b,c', ',', -1)",
    "SPLIT_PART(x, '::', 3)",
    f"EXTRACT(YEAR FROM {TS})",
    f"EXTRACT(MICROSECOND FROM {TS})",
    "CAST(2.5 AS DECIMAL(10, 2))",
    "CAST(2 AS DOUBLE PRECISION)",
    f"CAST({D} AS TIMESTAMP)",
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_the_allow_list_accepts_the_forms_that_agree(sql: str) -> None:
    assert problem(sql) is None, sql


def test_the_cast_types_left_out_are_not_on_the_list() -> None:
    names = {t.name for t in ALLOWED_CAST_TYPES}  # type: ignore[attr-defined]
    assert not names & {"CHAR", "TINYINT", "TIMESTAMPTZ"}, names
    assert {t.name for t in TEXT_CAST_TYPES} == {"TEXT", "VARCHAR"}  # type: ignore[attr-defined]


def test_the_refusals_reach_v05_through_parse_and_the_dialect_check() -> None:
    # `_parse` and `_dialect_problem` are the parts of V05 that run before the allow-list: they
    # must not crash on these spellings
    for sql in REFUSED:
        try:
            _parse(sql)
        except Exception as error:  # noqa: BLE001 - only a V05 issue is acceptable
            assert type(error).__name__ == "_ExprError", (sql, error)
        else:
            _dialect_problem(sql)


@pytest.mark.parametrize(
    "sql",
    ["CAST(x AS TEXT)", "CAST(x AS VARCHAR)", "x", "UPPER(x)"],
)
def test_text_casts_are_not_value_dependent(sql: str) -> None:
    assert first_value_dependent_cast(sqlglot.parse_one(sql, into=exp.Condition)) is None


@pytest.mark.parametrize("sql", ["CAST(x AS INT)", "CAST(x AS DATE)", "TS_OR_DS_TO_DATE(x)"])
def test_other_casts_are_value_dependent(sql: str) -> None:
    assert first_value_dependent_cast(sqlglot.parse_one(sql, into=exp.Condition)) is not None


def test_a_pg_spelled_int_type_name_is_refused_by_the_name_and_int8_by_the_cast_list() -> None:
    assert "int4" in (_dialect_problem("CAST(x AS int4)") or "")
    assert "int2" in (_dialect_problem("x::int2") or "")
    assert _dialect_problem("CAST(x AS INTEGER)") is None
    # int8 is read as TINYINT, which the allow-list refuses before the dialect check runs
    assert "TINYINT" in (problem("CAST(x AS int8)") or "")
    # and were TINYINT ever allowed again, the print comparison would still refuse int8
    assert "means something different" in (_dialect_problem("CAST(x AS int8)") or "")
