"""Running compiled SQL on the sample database (the evals only; the product never does, D-21).

The connection is the read-only role of `MAKNA_ENGINE_URL`, opened read-only with a statement
timeout. A failing statement raises `SqlFailure`, which carries the SQLSTATE and the class of the
error and keeps the engine's message apart: that text can hold a personal value
(`docs/notes/engine-error-text-can-carry-a-personal-value.md`), so the report and the default
output never show it.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import psycopg
import sqlglot
from sqlglot import exp

from evals.compare import Table

CATALOG = "lending"
"""The catalog name the compiler writes in front of `core.<table>`."""


class SqlFailure(Exception):
    """The engine refused or failed a statement. `str()` is the class and SQLSTATE only."""

    def __init__(self, sqlstate: str | None, error_class: str, message: str) -> None:
        super().__init__(f"{error_class} (SQLSTATE {sqlstate or 'unknown'})")
        self.sqlstate = sqlstate
        self.error_class = error_class
        self.message = message
        """The engine's own text. For standard error with `--verbose`; never for a report."""


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


class SqlRunner:
    """One read-only connection to the sample database, used for the whole run."""

    def __init__(self, url: str, statement_timeout_seconds: int) -> None:
        self._url = url
        self._timeout_ms = statement_timeout_seconds * 1000
        self._connection: psycopg.Connection[Any] | None = None
        self._database = ""

    def __enter__(self) -> SqlRunner:
        try:
            self._connection = psycopg.connect(
                self._url,
                autocommit=True,
                connect_timeout=10,
                options=(
                    f"-c default_transaction_read_only=on -c statement_timeout={self._timeout_ms}"
                ),
            )
            row = self._connection.execute("SELECT current_database()").fetchone()
        except psycopg.Error as error:
            raise SqlFailure(error.sqlstate, type(error).__name__, str(error)) from None
        assert row is not None
        self._database = str(row[0])
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            self._connection.close()

    def run(self, sql: str) -> Table:
        """Run one statement and return its columns and rows. Raises `SqlFailure`."""
        assert self._connection is not None
        try:
            cursor = self._connection.execute(runnable_sql(sql, self._database))
            columns = tuple(column.name for column in cursor.description or ())
            return Table(columns, tuple(tuple(row) for row in cursor.fetchall()))
        except psycopg.Error as error:
            raise SqlFailure(error.sqlstate, type(error).__name__, str(error)) from None
