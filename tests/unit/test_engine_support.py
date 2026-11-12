"""The engine-tier helper that makes compiled SQL runnable on a copy of the sample data."""

from __future__ import annotations

import pytest

from tests.engine.support import runnable_sql, same_rows

SQL = (
    'WITH "m" AS (SELECT SUM("t0"."x") AS "v" FROM "lending"."core"."t" AS "t0") '
    'SELECT "v" FROM "m" JOIN "lending"."core"."u" AS "t1" ON TRUE'
)


def test_in_a_database_called_lending_the_sql_is_untouched() -> None:
    assert runnable_sql(SQL, "lending") == SQL


def test_in_another_database_only_the_catalog_qualifier_is_removed() -> None:
    out = runnable_sql(SQL, "wp14_lending")
    assert '"lending"' not in out
    assert '"core"."t" AS "t0"' in out and '"core"."u" AS "t1"' in out
    assert 'FROM "m" JOIN' in out and 'SUM("t0"."x")' in out


def test_same_rows_ignores_order_and_number_types_but_not_values() -> None:
    from decimal import Decimal

    assert same_rows([("a", Decimal("1.5")), ("b", 2)], [("b", 2.0), ("a", 1.5)]) is None
    assert same_rows([("a", 1)], [("a", 2)]) is not None
    assert same_rows([("a", 1)], [("a", 1), ("b", 1)]) is not None
    assert same_rows([("a", None)], [("a", 1)]) is not None
    assert same_rows([("a", None)], [("a", None)]) is None


@pytest.mark.parametrize("a", [[("x", 1.0)], [(None, 1.0)]])
def test_same_rows_compares_text_exactly(a: list[tuple[object, ...]]) -> None:
    assert same_rows(a, [("y", 1.0)]) is not None
