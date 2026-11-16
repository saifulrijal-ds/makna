"""WP-15C fix round 1 (I-1): every allow-listed function means the same on PostgreSQL and DuckDB.

The unit tier runs compiled SQL on DuckDB, and the product prints for PostgreSQL, so a function
that sqlglot prints differently for the two engines gives a silent wrong number that no unit test
sees. Each case below is a minimal expression on fixed inputs. It is read with the neutral parser,
as a definition is, printed for each engine, and run: PostgreSQL through `MAKNA_ENGINE_URL`
(`SELECT` of constants only, nothing read from a table) and DuckDB in memory. The two values must
agree. A coverage test makes sure that every node class on the allow-list has a case, so a
function added to the list needs one here.
"""

from __future__ import annotations

import datetime
import decimal
from typing import Any

import duckdb
import psycopg
import pytest
import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from makna.definitions.validate import _dialect_problem
from makna.model.functions import ALLOWED_ANONYMOUS, ALLOWED_NODES, first_disallowed_function

D = "DATE '2026-03-15'"
TS = "TIMESTAMP '2026-03-15 10:20:30'"

# expression -> reason it is an expected difference (None: the engines must agree)
CASES: dict[str, str | None] = {
    # conditionals, casts, connectors
    "CAST(2.7 AS INTEGER)": None,
    "CAST('12' AS BIGINT)": None,
    "CAST(12 AS TEXT)": None,
    "CAST(12 AS VARCHAR)": None,
    "CAST('2026-03-15' AS DATE)": None,
    f"CAST({TS} AS DATE)": None,
    "CAST(1 AS BOOLEAN)": None,
    "CAST(2 AS DOUBLE PRECISION)": None,
    "CAST(2.5 AS DECIMAL(10, 2))": None,
    "CASE WHEN 1 > 2 THEN 'a' ELSE 'b' END": None,
    "IF(1 > 2, 'a', 'b')": None,
    "COALESCE(NULL, 3)": None,
    "NULLIF(3, 3)": None,
    "TRUE AND FALSE": None,
    "TRUE OR FALSE": None,
    "NOT TRUE": None,
    # arithmetic
    "ABS(-3)": None,
    "ROUND(2.567, 2)": None,
    "ROUND(2.5)": None,
    "ROUND(-2.5)": None,
    "FLOOR(-2.1)": None,
    "CEIL(2.1)": None,
    "SIGN(-4)": None,
    "SQRT(16)": None,
    "POWER(2, 10)": None,
    "MOD(10, 3)": None,
    "MOD(-10, 3)": None,
    "10 % 3": None,
    "7 / 2": None,
    "CAST(7 AS DOUBLE PRECISION) / 2": None,
    "-3 + 5 * 2": None,
    # text
    "LOWER('AbC')": None,
    "UPPER('AbC')": None,
    "INITCAP('hello world')": None,
    "TRIM('  x ')": None,
    "SUBSTRING('abcdef', 2, 3)": None,
    "SUBSTRING('abcdef', 4)": None,
    "LENGTH('héllo')": None,
    "'a' || 'b'": None,
    "CONCAT_WS('-', 'a', 'b')": None,
    "LEFT('abcdef', 2)": None,
    "RIGHT('abcdef', 2)": None,
    "REPLACE('abcabc', 'b', 'x')": None,
    "STR_POSITION('abcdef', 'cd')": None,
    "SPLIT_PART('a,b,c', ',', 2)": None,
    "LPAD('7', 3, '0')": None,
    "RPAD('7', 3, '0')": None,
    "BTRIM('xxhixx', 'x')": "DuckDB has no btrim; V05 refuses BTRIM anyway (it prints TRIM)",
    "LTRIM('xxhixx', 'x')": None,
    "RTRIM('xxhixx', 'x')": None,
    # dates
    f"DATE_TRUNC('month', {D})": None,
    f"DATE_TRUNC('week', {D})": None,
    f"DATE_TRUNC('quarter', {TS})": None,
    f"TIMESTAMP_TRUNC({TS}, MONTH)": None,
    f"EXTRACT(YEAR FROM {D})": None,
    f"EXTRACT(DOW FROM {D})": None,
    f"EXTRACT(DOY FROM {D})": None,
    f"YEAR({D})": None,
    f"MONTH({D})": None,
    f"DAY({D})": None,
    f"DATE_ADD({D}, 1, 'day')": None,
    f"DATE_SUB({D}, 1, 'month')": None,
    f"{D} + INTERVAL '1 day'": None,
    f"{D} - INTERVAL '1 month'": None,
    f"TS_OR_DS_ADD({D}, 2, 'day')": None,
    f"TS_OR_DS_TO_DATE({TS})": None,
}  # fmt: skip

# What a curator might write that the product now refuses, because the engines differ.
REFUSED = [
    f"DATE_ADD({D}, 1)",
    f"DATE_SUB({D}, 1)",
    f"TS_OR_DS_ADD({D}, 1)",
    f"DATEDIFF({TS}, TIMESTAMP '2026-03-14 23:00:00')",
    f"DAYOFWEEK({D})",
    f"DAYOFMONTH({D})",
    f"DAYOFYEAR({D})",
    "GREATEST(1, NULL)",  # PostgreSQL ignores NULL, DuckDB returns NULL
    "GREATEST(1, 5, 3)",
    "LEAST(4, NULL)",
    "LEAST(4, 2, 3)",
    f"DATE_PART('year', {D})",  # the two parses print it differently
    f"QUARTER({D})",  # PostgreSQL has no quarter(date)
    f"WEEK({D})",  # PostgreSQL has no week(date)
    f"TO_CHAR({D}, 'YYYY-MM')",  # sqlglot cannot print a format for DuckDB
]

# Allow-listed node classes that no neutral expression can reach, so no case can exist.
UNREACHABLE = {
    "Xor": "the neutral parser has no XOR operator",
    "ToChar": "sqlglot cannot print TO_CHAR with a format for DuckDB, so V05 refuses it",
}


def _print(sql: str, dialect: str) -> str:
    tree = sqlglot.parse_one(sql, into=exp.Condition)
    return "SELECT " + tree.sql(dialect=dialect, unsupported_level=ErrorLevel.RAISE)


def _plain(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    if isinstance(value, float):
        return round(value, 9)
    return value


def _both(connection: psycopg.Connection[Any], sql: str) -> tuple[Any, Any]:
    row = connection.execute(_print(sql, "postgres")).fetchone()
    assert row is not None
    mine = duckdb.connect().execute(_print(sql, "duckdb")).fetchone()
    assert mine is not None
    return _plain(row[0]), _plain(mine[0])


@pytest.mark.parametrize("sql", sorted(CASES), ids=str)
def test_the_engines_agree_on_an_allowed_function(
    engine_connection: psycopg.Connection[Any], sql: str
) -> None:
    reason = CASES[sql]
    if reason is not None:
        pytest.xfail(reason)
    postgres, mine = _both(engine_connection, sql)
    assert postgres == mine, (sql, postgres, mine)


@pytest.mark.parametrize("sql", sorted(CASES), ids=str)
def test_every_case_is_a_valid_definition_expression(sql: str) -> None:
    tree = sqlglot.parse_one(sql, into=exp.Condition)
    assert first_disallowed_function(tree) is None, sql


def test_every_function_on_the_allow_list_has_a_case() -> None:
    covered: set[type[exp.Expr]] = set()
    names: set[str] = set()
    for sql in CASES:
        for node in sqlglot.parse_one(sql, into=exp.Condition).walk():
            covered.add(type(node))
            if isinstance(node, exp.Anonymous):
                names.add(node.name.lower())
    missing = [
        n.__name__
        for n in ALLOWED_NODES
        if n.__name__ not in UNREACHABLE and not any(issubclass(c, n) for c in covered)
    ]
    assert not missing, f"allow-listed nodes with no case in this file: {missing}"
    # `ltrim` and `rtrim` parse to nodes, and `date_part` is refused (below); only `btrim` stays
    # an unknown name that the list must name
    assert {"btrim"} <= names and names <= ALLOWED_ANONYMOUS, names


@pytest.mark.parametrize("sql", REFUSED, ids=str)
def test_what_the_engines_read_differently_is_refused(sql: str) -> None:
    tree = sqlglot.parse_one(sql, into=exp.Condition)
    assert first_disallowed_function(tree) is not None or _dialect_problem(sql) is not None, sql


@pytest.mark.parametrize("sql", ["GREATEST(1, NULL)", "LEAST(4, NULL)"])
def test_greatest_and_least_are_refused_with_a_case_expression_as_the_alternative(
    sql: str,
) -> None:
    tree = sqlglot.parse_one(sql, into=exp.Condition)
    problem = first_disallowed_function(tree)
    assert problem is not None and "CASE" in problem, problem
