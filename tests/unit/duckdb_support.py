"""A DuckDB stand-in for the engine: print a compiler tree for DuckDB and run it (ARCHITECTURE 15).

The sample data lives in database `lending`, schema `core`, so the three-part table names of the
entity definitions (`lending.core.payments`) resolve exactly as they do on PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb
from sqlglot import exp

from data.generator.spec import TABLES
from makna.compiler.sqlgen import to_sql

Rows = list[tuple[Any, ...]]


def load_lending(directory: Path) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB holding the generated Parquet files as `lending.core.<table>`."""
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS lending")
    con.execute("CREATE SCHEMA lending.core")
    for spec in TABLES:
        path = (directory / f"{spec.name}.parquet").as_posix()
        con.execute(
            f"CREATE TABLE lending.core.{spec.name} AS SELECT * FROM read_parquet('{path}')"
        )
    con.execute("USE lending.core")
    return con


def run_tree(con: duckdb.DuckDBPyConnection, tree: exp.Expr) -> Rows:
    """Print `tree` for DuckDB and run it."""
    return con.execute(to_sql(tree, "duckdb")).fetchall()


def run_sql(con: duckdb.DuckDBPyConnection, sql: str) -> Rows:
    return con.execute(sql).fetchall()


def same_rows(got: Sequence[Sequence[Any]], want: Sequence[Sequence[Any]]) -> bool:
    """Rows equal in order; numbers match to a relative 1e-9 (floats and decimals mix)."""
    if len(got) != len(want):
        return False
    for g_row, w_row in zip(got, want, strict=True):
        if len(g_row) != len(w_row):
            return False
        for g, w in zip(g_row, w_row, strict=True):
            if g is None or w is None:
                if g is not w:
                    return False
            elif isinstance(g, (int, float)) or hasattr(g, "as_tuple"):
                if abs(float(g) - float(w)) > 1e-9 * max(1.0, abs(float(w))):
                    return False
            elif g != w:
                return False
    return True
