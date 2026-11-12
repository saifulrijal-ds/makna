"""Engine-tier helper: run the SQL the compiler printed on the sample database (tests only).

No product code path runs a caller's SQL (D-21); this is how the tests check that the SQL the
compiler produces works on PostgreSQL.
"""

from __future__ import annotations

from typing import Any

import psycopg
import sqlglot
from sqlglot import exp

CATALOG = "lending"


def runnable_sql(sql: str, database: str) -> str:
    """`sql` as it can run in `database`.

    The compiler writes tables as `lending.core.<table>`. PostgreSQL accepts the three-part name
    only when the connected database is called `lending` (the dev stack and CI), and then the SQL
    is returned untouched, so the real text is what runs. A copy of the sample data under another
    name (a local PostgreSQL) cannot resolve it, so the `lending` qualifier is removed from the
    tables and nothing else changes.
    """
    if database == CATALOG:
        return sql
    tree = sqlglot.parse_one(sql, read="postgres")
    for table in tree.find_all(exp.Table):
        if table.catalog.lower() == CATALOG:
            table.set("catalog", None)
    return tree.sql(dialect="postgres")


def run_compiled(
    connection: psycopg.Connection[Any], sql: str
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run compiled SQL; return the result column names and the rows."""
    row = connection.execute("SELECT current_database()").fetchone()
    assert row is not None
    cursor = connection.execute(runnable_sql(sql, row[0]))
    names = [column.name for column in cursor.description or []]
    return names, [tuple(r) for r in cursor.fetchall()]


def run_reference(
    connection: psycopg.Connection[Any], sql: str
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run a hand-written reference query (names tables as `core.<table>`)."""
    cursor = connection.execute(sql)
    return [column.name for column in cursor.description or []], [
        tuple(r) for r in cursor.fetchall()
    ]


def same_rows(got: list[tuple[Any, ...]], want: list[tuple[Any, ...]]) -> str | None:
    """None if both hold the same rows, else what differs. Row order is not compared: sorting
    text follows the database locale. Numbers match to 1e-9 relative, as in the spike."""
    if len(got) != len(want):
        return f"{len(got)} rows, expected {len(want)}"

    def key(row: tuple[Any, ...]) -> tuple[str, ...]:
        return tuple("" if v is None else str(v) for v in row if not _is_number(v))

    for index, (a, b) in enumerate(zip(sorted(got, key=key), sorted(want, key=key), strict=True)):
        if len(a) != len(b):
            return f"row {index}: {len(a)} columns, expected {len(b)}"
        for x, y in zip(a, b, strict=True):
            if _is_number(x) or _is_number(y):
                if x is None or y is None:
                    if x is not y:
                        return f"row {index}: {a!r} != {b!r}"
                elif abs(float(x) - float(y)) > 1e-9 * max(1.0, abs(float(x))):
                    return f"row {index}: {a!r} != {b!r}"
            elif x != y:
                return f"row {index}: {a!r} != {b!r}"
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) or hasattr(value, "as_tuple")
