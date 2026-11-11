"""Create the `lending.core` tables from the table specification and load the Parquet files.

Run against the `lending` database with the loader role (the owner of the tables). Reusable:
a table is reloaded only when the Parquet file it was loaded from changed, so running the
loader again changes nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pyarrow.parquet as pq
from psycopg import sql

from data.generator.spec import TABLES, TableSpec

SCHEMA = "core"
COMMENT_PREFIX = "makna-sample-data sha256:"


def create_table_sql(spec: TableSpec) -> sql.Composed:
    columns = [
        sql.SQL("{} {}").format(sql.Identifier(c.name), sql.SQL(c.sql_type)) for c in spec.columns
    ]
    primary_key = sql.SQL("PRIMARY KEY ({})").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in spec.primary_key)
    )
    return sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
        sql.Identifier(SCHEMA, spec.name), sql.SQL(", ").join([*columns, primary_key])
    )


def create_tables(connection: psycopg.Connection) -> None:
    """Create the schema and the nine tables if they are missing."""
    connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    for spec in TABLES:
        connection.execute(create_table_sql(spec))


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _loaded_digest(connection: psycopg.Connection, spec: TableSpec) -> str | None:
    row = connection.execute(
        "SELECT obj_description(to_regclass(%s), 'pg_class')", (f"{SCHEMA}.{spec.name}",)
    ).fetchone()
    comment = row[0] if row else None
    if isinstance(comment, str) and comment.startswith(COMMENT_PREFIX):
        return comment[len(COMMENT_PREFIX) :]
    return None


def _rows(path: Path) -> Iterator[tuple[object, ...]]:
    for batch in pq.ParquetFile(path).iter_batches(batch_size=20_000):
        columns = [column.to_pylist() for column in batch.columns]
        yield from zip(*columns, strict=True)


def load_table(connection: psycopg.Connection, spec: TableSpec, path: Path) -> bool:
    """Load one table from its Parquet file. Returns False when it is already loaded."""
    digest = file_digest(path)
    if _loaded_digest(connection, spec) == digest:
        return False
    target = sql.Identifier(SCHEMA, spec.name)
    connection.execute(sql.SQL("TRUNCATE {}").format(target))
    names = sql.SQL(", ").join(sql.Identifier(c) for c in spec.column_names)
    with connection.cursor() as cursor:
        with cursor.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(target, names)) as copy:
            for row in _rows(path):
                copy.write_row(row)
    connection.execute(
        sql.SQL("COMMENT ON TABLE {} IS {}").format(target, sql.Literal(COMMENT_PREFIX + digest))
    )
    return True


def load_all(url: str, directory: Path) -> dict[str, bool]:
    """Create the tables and load every file. Returns table name -> whether it was (re)loaded.

    Each table loads in its own transaction, so a failure leaves finished tables in place and
    the next run redoes only the unfinished ones.
    """
    loaded: dict[str, bool] = {}
    with psycopg.connect(url) as connection:
        create_tables(connection)
        connection.commit()
        for spec in TABLES:
            loaded[spec.name] = load_table(connection, spec, directory / f"{spec.name}.parquet")
            connection.commit()
    return loaded


def row_counts(url: str) -> dict[str, int]:
    with psycopg.connect(url) as connection:
        return {spec.name: _count(connection, spec.name) for spec in TABLES}


def _count(connection: psycopg.Connection, table: str) -> int:
    row = connection.execute(
        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(SCHEMA, table))
    ).fetchone()
    assert row is not None
    return int(row[0])
