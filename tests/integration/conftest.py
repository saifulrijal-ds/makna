"""Integration tier: a scratch database on the PostgreSQL named by MAKNA_TEST_DATABASE_URL.

The role in that URL needs CREATEDB and the right to create the `vector` extension (in CI, the
superuser of the pgvector service container). Each test gets its own empty database, dropped
afterwards, so tests never see each other's tables.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from makna.index.db import sqlalchemy_url
from makna.model.search import EmbeddingProvider
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


def _build_sample_index(url: URL, provider: EmbeddingProvider | None = None) -> None:
    """Migrate, sync the sample catalog, and index the sample definitions with `hash`."""
    from makna.catalog.file import FileConnector
    from makna.catalog.sync import sync_catalog
    from makna.definitions.loader import definition_objects, load_definitions
    from makna.index import indexer, migrate
    from makna.search.embedding import HashProvider
    from tests.catalog_support import DEFINITIONS, EXPORT, TAG_MAP, sample_objects

    migrate.upgrade(url.render_as_string(hide_password=False))
    engine = create_engine(url)
    try:
        entities, terms = sample_objects()
        sync_catalog(
            engine, FileConnector(EXPORT), tag_map=TAG_MAP, entities=entities, glossary_terms=terms
        )
        objects = definition_objects(load_definitions(DEFINITIONS))
        indexer.build(engine, provider or HashProvider(), objects)
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def indexed_engine(test_database_url: URL) -> Iterator[Engine]:
    """A database with the sample catalog and definitions indexed (hash provider).

    Shared by the tests of one module, so they must only read it.
    """
    name = f"makna_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(test_database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = test_database_url.set(database=name)
    try:
        _build_sample_index(url)
        engine = create_engine(url)
        yield engine
        engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def fresh_indexed_engine(empty_database: URL) -> Iterator[Engine]:
    """Like `indexed_engine`, but private to one test, which may change it."""
    _build_sample_index(empty_database)
    engine = create_engine(empty_database)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def service_world(test_database_url: URL) -> Iterator[tuple[Engine, Any]]:
    """A database for the service tests: sample catalog and definitions indexed with the service
    hook under `strict`, four users, and a token. Shared by one module, so tests must not change
    the definitions, the catalog or the index (they write `tool_call_log` rows only)."""
    from . import service_support

    name = f"makna_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(test_database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = test_database_url.set(database=name)
    try:
        users = service_support.build(url)
        engine = create_engine(url)
        yield engine, users
        engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def fresh_service_world(empty_database: URL) -> Iterator[tuple[Engine, Any]]:
    """Like `service_world`, private to one test, which may change anything."""
    from . import service_support

    users = service_support.build(empty_database)
    engine = create_engine(empty_database)
    yield engine, users
    engine.dispose()
