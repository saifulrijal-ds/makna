"""WP-14 acceptance 3: every reference SQL runs on PostgreSQL, returns 1 to 200 rows, and gives
the committed CSV again. The comparison is exact text (docs/EVALS.md section 2.3)."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from evals.golden.reference import (
    compare_with_expected,
    reference_sql_files,
    row_count_problem,
    run_reference_sql,
)

FILES = reference_sql_files()


def test_there_are_reference_files() -> None:
    assert len(FILES) >= 40


@pytest.mark.parametrize("sql_file", FILES, ids=lambda path: path.stem)
def test_reference_sql_runs_and_matches_its_csv(
    engine_connection: psycopg.Connection, sql_file: Path
) -> None:
    result = run_reference_sql(engine_connection, sql_file)
    assert row_count_problem(sql_file.stem, result) is None
    assert compare_with_expected(engine_connection, sql_file) is None
