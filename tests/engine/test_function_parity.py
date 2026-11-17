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
import re
from typing import Any

import duckdb
import psycopg
import pytest
import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from makna.model.functions import (
    ALLOWED_ANONYMOUS,
    ALLOWED_CAST_TYPES,
    ALLOWED_EXTRACT_PARTS,
    ALLOWED_NODES,
    first_disallowed_function,
)

D = "DATE '2026-03-15'"
TS = "TIMESTAMP '2026-03-15 10:20:30'"
TSF = "TIMESTAMP '2026-03-15 10:20:30.5'"

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
    "CAST(300 AS SMALLINT)": None,
    "CAST(1.5 AS FLOAT)": None,
    "CAST(2.5 AS DECIMAL(3, 0))": None,
    "CAST(1.5 AS TEXT)": None,
    "CAST('  abc ' AS VARCHAR)": None,
    f"CAST({D} AS TIMESTAMP)": None,
    "CAST(TRUE AS INTEGER)": None,
    "CASE WHEN 1 > 2 THEN 'a' ELSE 'b' END": None,
    "IF(1 > 2, 'a', 'b')": None,
    "COALESCE(NULL, 3)": None,
    "NULLIF(3, 3)": None,
    "TRUE AND FALSE": None,
    "TRUE OR FALSE": None,
    "NOT TRUE": None,
    # arithmetic
    "ABS(-3)": None,
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
    "SUBSTRING('abcdef', 2, 0)": None,
    "SUBSTRING('abcdef', 10, 3)": None,
    "SUBSTRING('abcdef' FROM 2 FOR 3)": None,
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
    # known limits (ARCHITECTURE 5.1): the engines differ, nothing refuses these
    "UPPER('ß')": "case mapping of non-ASCII text differs (known limit)",
    "CAST(1e20 AS TEXT)": "the text form of a float differs (known limit)",
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
    f"DATE_PART('year', {D})": None,
    f"DATE_PART('quarter', {TSF})": None,
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

# Every part of EXTRACT that the product allows, on a timestamp with fractional seconds.
CASES.update({f"EXTRACT({part} FROM {TSF})": None for part in sorted(ALLOWED_EXTRACT_PARTS)})

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


def _params() -> list[Any]:
    return [
        pytest.param(sql, id=sql, marks=pytest.mark.xfail(strict=True, reason=reason))
        if reason is not None
        else pytest.param(sql, id=sql)
        for sql, reason in sorted(CASES.items())
    ]


@pytest.mark.parametrize("sql", _params())
def test_the_engines_agree_on_an_allowed_function(
    engine_connection: psycopg.Connection[Any], sql: str
) -> None:
    postgres, mine = _both(engine_connection, sql)
    assert postgres == mine, (sql, postgres, mine)


@pytest.mark.parametrize("sql", sorted(CASES), ids=str)
def test_every_case_is_a_valid_definition_expression(sql: str) -> None:
    tree = sqlglot.parse_one(sql, into=exp.Condition)
    assert first_disallowed_function(tree) is None, sql


def test_every_function_on_the_allow_list_has_a_case() -> None:
    covered: set[type[exp.Expr]] = set()
    for sql in CASES:
        for node in sqlglot.parse_one(sql, into=exp.Condition).walk():
            covered.add(type(node))
    missing = [
        n.__name__
        for n in ALLOWED_NODES
        if n.__name__ not in UNREACHABLE and not any(issubclass(c, n) for c in covered)
    ]
    assert not missing, f"allow-listed nodes with no case in this file: {missing}"


def test_every_cast_type_on_the_allow_list_has_a_case() -> None:
    covered = {
        node.args["to"].this
        for sql in CASES
        for node in sqlglot.parse_one(sql, into=exp.Condition).walk()
        if isinstance(node, exp.Cast) and isinstance(node.args.get("to"), exp.DataType)
    }
    assert not set(ALLOWED_CAST_TYPES) - covered, set(ALLOWED_CAST_TYPES) - covered


def test_every_function_name_on_the_allow_list_has_a_case() -> None:
    text = "\n".join(CASES).lower()
    missing = [n for n in sorted(ALLOWED_ANONYMOUS) if not re.search(rf"\b{n}\(", text)]
    assert not missing, f"allow-listed names with no case in this file: {missing}"
