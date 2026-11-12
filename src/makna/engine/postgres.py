"""The PostgreSQL connector: table introspection and the freshness query.

It connects with the read-only service account of `MAKNA_ENGINE_URL`. Every connection is opened
read-only and with a statement timeout, and runs only the two fixed statements below. The
freshness query is a sqlglot tree like every other query Makna builds; no SQL text is put
together from parts.

Table names are given as `schema.table` or `catalog.schema.table` (`engine_fqn`). The catalog
part is dropped: a connection reads one database, and PostgreSQL accepts a catalog qualifier
only when it equals that database's name, which a copy of the sample data may not.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg import errors
from sqlglot import exp

from makna.engine.base import EngineColumn, EngineError, EngineTable, redact_url, scrub

log = logging.getLogger(__name__)

# 30 seconds: ample for the one aggregate of the freshness check on a large indexed or
# partitioned fact table, short enough that a refresh job over every measure cannot hang on a
# table that is being rebuilt. The setting is `[engine] statement_timeout_seconds`.
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 30
CONNECT_TIMEOUT_SECONDS = 10

_LIST_TABLES = """
SELECT c.relname, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod)
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = %s
  AND c.relkind IN ('r', 'p')
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""


class PostgresConnector:
    """Read-only access to one PostgreSQL database for introspection and freshness."""

    dialect = "postgres"

    def __init__(
        self,
        url: str,
        *,
        statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS,
    ) -> None:
        if statement_timeout_seconds < 1:
            raise EngineError("The statement timeout must be at least 1 second.")
        self._url = url
        self._timeout = statement_timeout_seconds

    @property
    def target(self) -> str:
        """Where the connector connects, without the password."""
        return redact_url(self._url)

    def list_tables(self, schema: str) -> list[EngineTable]:
        rows = self._fetch(_LIST_TABLES, (schema,))
        tables: dict[str, list[EngineColumn]] = {}
        for table, column, native_type in rows:
            tables.setdefault(table, []).append(EngineColumn(column, native_type))
        return [EngineTable(schema, name, tuple(columns)) for name, columns in tables.items()]

    def latest_value(self, table: str, expr: exp.Expr) -> date | None:
        schema, name = _split_table(table)
        tree = exp.select(exp.Max(this=expr.copy())).from_(exp.table_(name, db=schema, quoted=True))
        rows = self._fetch(tree.sql(dialect=self.dialect), ())
        value = rows[0][0] if rows else None
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        raise EngineError(_not_a_date(schema, name, value))

    # ---- connection ------------------------------------------------------------------

    def _fetch(self, statement: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement, params)
                    return [tuple(row) for row in cursor.fetchall()]
        except errors.QueryCanceled:
            message = _timeout_message(self._timeout, self.target)
            log.warning("%s", message)
            raise EngineError(message) from None
        except psycopg.Error as error:
            raise self._failure("The engine query failed", error) from None

    def _connect(self) -> psycopg.Connection[Any]:
        try:
            connection = psycopg.connect(self._url, connect_timeout=CONNECT_TIMEOUT_SECONDS)
        except psycopg.Error as error:
            raise self._failure("Cannot connect to the engine", error) from None
        except Exception as error:  # a malformed URL raises ProgrammingError or ValueError
            raise self._failure("Cannot connect to the engine", error) from None
        try:
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (str(self._timeout * 1000),),
                )
            connection.commit()
        except psycopg.Error as error:
            connection.close()
            raise self._failure("Cannot prepare the engine connection", error) from None
        return connection

    def _failure(self, what: str, error: BaseException) -> EngineError:
        """An `EngineError` that names the target without its password; the log record holds
        the same text."""
        message = _failure_message(what, self.target, scrub(str(error), self._url))
        log.warning("%s", message)
        return EngineError(message)


def _split_table(table: str) -> tuple[str, str]:
    parts = table.split(".")
    if len(parts) not in (2, 3) or not all(parts):
        raise EngineError(_bad_table(table))
    return parts[-2], parts[-1]


# Message texts for `EngineError`, kept in their own functions: they are the only places in this
# module that format text, and none of them builds SQL (tests/unit/test_engine_no_sql_strings.py).


def _timeout_message(seconds: int, target: str) -> str:
    return (
        f"The engine did not answer within the statement timeout of {seconds} seconds "
        f"({target}). Raise `[engine] statement_timeout_seconds` in config/makna.toml if the "
        "table is large."
    )


def _failure_message(what: str, target: str, detail: str) -> str:
    return f"{what} ({target}): {' '.join(detail.split())}"


def _not_a_date(schema: str, name: str, value: object) -> str:
    return (
        f"The newest value of the expression on {schema}.{name} is not a date or a timestamp "
        f"(got {type(value).__name__})."
    )


def _bad_table(table: str) -> str:
    return f"Table name {table!r} must be schema.table or catalog.schema.table."
