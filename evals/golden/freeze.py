"""Write the expected CSVs of the golden set: `uv run python -m evals.golden.freeze`.

Reads `MAKNA_ENGINE_URL` (the `lending` database as role `lending_reader`, as the app does),
runs every file in `evals/golden/reference_sql/` and rewrites `evals/golden/expected/`. Run it
only after a deliberate change to the data generator or to a definition; the engine test
`tests/engine/test_golden_reference.py` fails when a committed CSV no longer matches.

This is a stop-gap for WP-14. WP-15 replaces it with `makna-eval freeze` and reuses
`evals.golden.reference` unchanged.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

import psycopg

from evals.golden.reference import EXPECTED_DIR, reference_sql_files, write_expected


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    url = os.environ.get("MAKNA_ENGINE_URL")
    if not url:
        print("freeze: MAKNA_ENGINE_URL is not set (the lending database, read-only role)")
        return 2
    files = reference_sql_files()
    if not files:
        print("freeze: no reference SQL files found")
        return 2
    failed = 0
    with psycopg.connect(url) as connection:
        for sql_file in files:
            try:
                result = write_expected(connection, sql_file)
            except (ValueError, psycopg.Error) as error:
                failed += 1
                print(f"freeze: {sql_file.stem}: FAILED: {error}")
                connection.rollback()
                continue
            print(f"freeze: {sql_file.stem}: {len(result.rows)} rows")
    print(f"freeze: wrote {len(files) - failed} of {len(files)} files to {EXPECTED_DIR}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
