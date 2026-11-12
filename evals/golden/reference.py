"""Run a reference SQL file and turn its result into the text of an expected CSV.

This is the one shared function of the golden set: `python -m evals.golden.freeze` writes the
CSVs with it and the engine test re-runs every reference SQL with it and compares exact text.
WP-15 folds it into `makna-eval freeze`; until then nothing else uses it.

CSV format (fixed so that a re-run compares as plain text): a header row of column names, one
row per result row in the order of the SQL's own ORDER BY, values in the form psycopg returns
them (`Decimal` and floats through `str`, dates as ISO text, booleans as `true`/`false`), NULL
as an empty string, UTF-8, `\\n` line endings.
"""

from __future__ import annotations

import csv
import datetime
import io
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import psycopg

GOLDEN_DIR = Path(__file__).resolve().parent
QUESTIONS_FILE = GOLDEN_DIR / "questions.yaml"
SQL_DIR = GOLDEN_DIR / "reference_sql"
EXPECTED_DIR = GOLDEN_DIR / "expected"

MIN_ROWS = 1
MAX_ROWS = 200


@dataclass(frozen=True)
class ReferenceResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


def reference_sql_files(directory: Path = SQL_DIR) -> list[Path]:
    return sorted(directory.glob("*.sql"))


def run_reference_sql(connection: psycopg.Connection, sql_file: Path) -> ReferenceResult:
    """Execute one reference SQL file and return its columns and rows."""
    cursor = connection.execute(sql_file.read_text(encoding="utf-8"))
    columns = tuple(column.name for column in cursor.description or ())
    return ReferenceResult(columns, tuple(tuple(row) for row in cursor.fetchall()))


def format_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime.date | datetime.datetime):
        return value.isoformat()
    if isinstance(value, Decimal | int | float | str):
        return str(value)
    raise TypeError(f"no CSV form for {type(value).__name__}: {value!r}")


def render_csv(result: ReferenceResult) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(result.columns)
    for row in result.rows:
        writer.writerow([format_value(v) for v in row])
    return buffer.getvalue()


def row_count_problem(name: str, result: ReferenceResult) -> str | None:
    n = len(result.rows)
    if MIN_ROWS <= n <= MAX_ROWS:
        return None
    return f"{name}: {n} rows; a reference result needs {MIN_ROWS} to {MAX_ROWS}"


def expected_path(sql_file: Path, directory: Path = EXPECTED_DIR) -> Path:
    return directory / f"{sql_file.stem}.csv"


def compare_with_expected(
    connection: psycopg.Connection,
    sql_file: Path,
    directory: Path = EXPECTED_DIR,
) -> str | None:
    """Re-run `sql_file` and compare its CSV text with the committed file. None means equal."""
    result = run_reference_sql(connection, sql_file)
    if (problem := row_count_problem(sql_file.stem, result)) is not None:
        return problem
    target = expected_path(sql_file, directory)
    if not target.exists():
        return f"{sql_file.stem}: no expected file {target.name}"
    committed = target.read_bytes().decode("utf-8")
    if render_csv(result) != committed:
        return f"{sql_file.stem}: result differs from {target.name}"
    return None


def write_expected(
    connection: psycopg.Connection,
    sql_file: Path,
    directory: Path = EXPECTED_DIR,
) -> ReferenceResult:
    """Run `sql_file` and write its CSV. Raises ValueError when the row count is out of range."""
    result = run_reference_sql(connection, sql_file)
    if (problem := row_count_problem(sql_file.stem, result)) is not None:
        raise ValueError(problem)
    directory.mkdir(parents=True, exist_ok=True)
    expected_path(sql_file, directory).write_bytes(render_csv(result).encode("utf-8"))
    return result
