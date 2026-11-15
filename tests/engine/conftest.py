"""Engine tier: the sample data in PostgreSQL, read as the `lending_reader` role.

`MAKNA_ENGINE_URL` names the `lending` database (the generated sample data in schema `core`).
Without it every engine test skips; with `MAKNA_REQUIRE_ENGINE_DATABASE` set (CI) a missing URL
fails instead, so a job that lost its database cannot pass by skipping everything.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from evals.runtime import Runtime
from makna.index.db import sqlalchemy_url
from makna.model.governance import STRICT
from makna.model.search import SearchSettings
from makna.search.embedding import HashProvider
from tests import db_env
from tests.integration import service_support


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


@pytest.fixture(scope="module")
def eval_runtime(engine_url: str) -> Iterator[Runtime]:
    """What the compile evals need: a scratch application database with the sample catalog and
    definitions indexed (hash embeddings, strict policy), and the sample database to run on.

    Needs both `MAKNA_TEST_DATABASE_URL` (a PostgreSQL with pgvector, as the integration tier) and
    `MAKNA_ENGINE_URL`; a missing test database is a skip, or a failure when
    `MAKNA_REQUIRE_TEST_DATABASE` is set (CI).
    """
    value, problem = db_env.find_test_database_url(os.environ)
    if value is None:
        assert problem is not None
        if problem.startswith("FAIL"):
            pytest.fail(problem, pytrace=False)
        pytest.skip(problem)
    base = make_url(sqlalchemy_url(value))
    name = f"makna_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name)
    try:
        service_support.build(url)
        engine = create_engine(url)
        yield Runtime(
            engine=engine,
            provider=HashProvider(),
            provider_name="hash",
            search=SearchSettings(),
            policy=STRICT,
            dialect="postgres",
            engine_url=engine_url,
            statement_timeout_seconds=30,
        )
        engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
