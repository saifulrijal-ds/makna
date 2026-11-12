"""WP-11 acceptance 5, second half: `makna catalog compare` against a scratch copy of the sample
tables, with a column dropped.

The catalog is the synced copy in a scratch `makna` database (as `makna catalog issues` reads
it). The engine is a second scratch database holding the nine `core` tables created from the
generator's table specification, with no rows: the comparison reads names only. The sample
database itself is never written to; its "no differences" half is in the engine tier
(`tests/engine/test_catalog_compare.py`) and in the CI stack job.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import URL, Engine, create_engine, text

from makna.cli.main import main

from ..catalog_support import DEFINITIONS, EXPORT


def plain(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def scratch_engine(test_database_url: URL) -> Iterator[URL]:
    """A database with the empty `core` tables of the sample data. Dropped afterwards."""
    from data.lending_loader import create_tables

    name = f"lending_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(test_database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = test_database_url.set(database=name)
    try:
        with psycopg.connect(plain(url), autocommit=True) as connection:
            create_tables(connection)
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def synced(
    migrated_url: URL,
    migrated_engine: Engine,
    scratch_engine: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> URL:
    """The sample catalog synced into the scratch makna database; both URLs in the environment.
    Returns the engine database URL."""
    monkeypatch.setenv("MAKNA_DATABASE_URL", plain(migrated_url))
    monkeypatch.setenv("MAKNA_ENGINE_URL", plain(scratch_engine))
    code, _, err = run(
        capsys, "catalog", "sync", "--file", str(EXPORT), "--definitions", str(DEFINITIONS)
    )
    assert code == 0, err
    return scratch_engine


def alter(engine_url: URL, statement: str) -> None:
    with psycopg.connect(plain(engine_url), autocommit=True) as connection:
        connection.execute(statement)


def test_a_copy_that_matches_the_catalog_has_no_differences(
    synced: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, "catalog", "compare")
    assert code == 0, err
    assert "no differences" in out
    assert "catalog tables: 9, engine tables read: 9" in out


def test_a_dropped_column_is_reported_as_missing(
    synced: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    alter(synced, "ALTER TABLE core.customers DROP COLUMN city")
    code, out, err = run(capsys, "catalog", "compare")
    assert code == 1, err
    assert "missing_column core.customers.city: in the catalog, not in the engine" in out
    lines = [line for line in out.splitlines() if line.startswith(("missing_", "extra_"))]
    assert len(lines) == 1
    assert "no differences" not in out


def test_a_dropped_table_an_added_column_and_an_added_table_are_reported(
    synced: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    alter(synced, "DROP TABLE core.collection_actions")
    alter(synced, "ALTER TABLE core.branches ADD COLUMN manager text")
    alter(synced, "CREATE TABLE core.scratch_notes (id integer)")
    code, out, _ = run(capsys, "catalog", "compare")
    assert code == 1
    assert "missing_table core.collection_actions: in the catalog, not in the engine" in out
    assert "extra_column core.branches.manager: in the engine, not in the catalog" in out
    assert "extra_table core.scratch_notes: in the engine, not in the catalog" in out


def test_the_output_names_the_engine_database_and_never_its_password(
    synced: URL, test_database_url: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    alter(synced, "ALTER TABLE core.customers DROP COLUMN city")
    code, out, err = run(capsys, "catalog", "compare")
    assert code == 1
    assert "engine: " in out and (synced.database or "") in out
    password = test_database_url.password
    if password:
        assert password not in out + err


def test_an_engine_that_cannot_be_reached_exits_2_and_never_prints_the_password(
    synced: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bogus = "wp11-not-the-password"
    unreachable = synced.set(database="wp11_no_such_database", password=bogus)
    monkeypatch.setenv("MAKNA_ENGINE_URL", plain(unreachable))
    code, out, err = run(capsys, "catalog", "compare")
    assert code == 2 and out == ""
    assert "makna catalog compare:" in err and "wp11_no_such_database" in err
    assert bogus not in out + err


def test_an_empty_catalog_copy_is_an_error_not_no_differences(
    migrated_url: URL,
    migrated_engine: Engine,
    scratch_engine: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A database that was never synced has no catalog tables; "no differences" would be false."""
    monkeypatch.setenv("MAKNA_DATABASE_URL", plain(migrated_url))
    monkeypatch.setenv("MAKNA_ENGINE_URL", plain(scratch_engine))
    code, out, err = run(capsys, "catalog", "compare")
    assert code == 2
    assert "no differences" not in out
    assert "catalog copy has no tables" in err and "makna catalog sync" in err
