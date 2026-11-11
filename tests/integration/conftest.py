"""Integration tier: a scratch database on the PostgreSQL named by MAKNA_TEST_DATABASE_URL.

The role in that URL needs CREATEDB and the right to create the `vector` extension (in CI, the
superuser of the pgvector service container). Each test gets its own empty database, dropped
afterwards, so tests never see each other's tables.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from makna.index.db import sqlalchemy_url
from tests import db_env


@pytest.fixture(scope="session")
def test_database_url() -> URL:
    value, problem = db_env.find_test_database_url(os.environ)
    if value is None:
        assert problem is not None
        if problem.startswith("FAIL"):
            pytest.fail(problem, pytrace=False)
        pytest.skip(problem)
    return make_url(sqlalchemy_url(value))


@pytest.fixture
def empty_database(test_database_url: URL) -> Iterator[URL]:
    """URL of a new empty database on the test server."""
    name = f"makna_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(test_database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield test_database_url.set(database=name)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def engine(empty_database: URL) -> Iterator[Engine]:
    engine = create_engine(empty_database)
    yield engine
    engine.dispose()


@pytest.fixture
def migrated_url(empty_database: URL) -> URL:
    """The scratch database after `alembic upgrade head`."""
    from makna.index import migrate

    migrate.upgrade(empty_database.render_as_string(hide_password=False))
    return empty_database


@pytest.fixture
def migrated_engine(migrated_url: URL) -> Iterator[Engine]:
    engine = create_engine(migrated_url)
    yield engine
    engine.dispose()
