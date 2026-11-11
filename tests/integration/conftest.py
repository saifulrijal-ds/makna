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


@pytest.fixture(scope="session")
def test_database_url() -> URL:
    value = os.environ.get("MAKNA_TEST_DATABASE_URL")
    if not value:
        pytest.skip("MAKNA_TEST_DATABASE_URL is not set (needs PostgreSQL with pgvector)")
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
