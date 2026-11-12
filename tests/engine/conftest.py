"""Engine tier: the sample data in PostgreSQL, read as the `lending_reader` role.

`MAKNA_ENGINE_URL` names the `lending` database (the generated sample data in schema `core`).
Without it every engine test skips; with `MAKNA_REQUIRE_ENGINE_DATABASE` set (CI) a missing URL
fails instead, so a job that lost its database cannot pass by skipping everything.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

from tests import db_env


@pytest.fixture(scope="session")
def engine_url() -> str:
    value, problem = db_env.find_engine_url(os.environ)
    if value is None:
        assert problem is not None
        if problem.startswith("FAIL"):
            pytest.fail(problem, pytrace=False)
        pytest.skip(problem)
    return value


@pytest.fixture(scope="session")
def engine_connection(engine_url: str) -> Iterator[psycopg.Connection]:
    """One autocommit connection for the whole session; engine tests only read."""
    with psycopg.connect(engine_url, autocommit=True) as connection:
        yield connection
