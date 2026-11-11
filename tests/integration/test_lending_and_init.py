"""WP-05 criteria 2 and 4 on a scratch PostgreSQL: the loader and the whole `init` run."""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from data.generator import DEFAULT_SEED, TABLES, generate, write_parquet
from data.lending_loader import load_all, row_counts
from deploy.init import STEPS, Context, run_steps


@pytest.fixture(scope="module")
def parquet_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("lending_files")
    write_parquet(generate(DEFAULT_SEED, 0.05), out)
    return out


@pytest.fixture
def lending_database(test_database_url: URL) -> Iterator[URL]:
    name = f"lending_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(test_database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield test_database_url.set(database=name)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def plain(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def test_row_counts_equal_the_parquet_files(lending_database: URL, parquet_dir: Path) -> None:
    url = plain(lending_database)
    load_all(url, parquet_dir)
    expected = {
        t.name: pq.ParquetFile(parquet_dir / f"{t.name}.parquet").metadata.num_rows for t in TABLES
    }
    assert len(expected) == 9
    assert row_counts(url) == expected


def test_second_load_changes_nothing_and_a_changed_file_reloads(
    lending_database: URL, parquet_dir: Path, tmp_path: Path
) -> None:
    url = plain(lending_database)
    assert all(load_all(url, parquet_dir).values())
    assert not any(load_all(url, parquet_dir).values())

    other = tmp_path / "other"
    write_parquet(generate(DEFAULT_SEED + 1, 0.05), other)
    changed = load_all(url, other)
    assert any(changed.values())
    expected = {
        t.name: pq.ParquetFile(other / f"{t.name}.parquet").metadata.num_rows for t in TABLES
    }
    assert row_counts(url) == expected


def test_a_partly_loaded_table_is_repaired(lending_database: URL, parquet_dir: Path) -> None:
    url = plain(lending_database)
    load_all(url, parquet_dir)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("DELETE FROM core.payments WHERE payment_id % 2 = 0")
        connection.execute("COMMENT ON TABLE core.payments IS NULL")
    assert load_all(url, parquet_dir)["payments"] is True
    full = pq.ParquetFile(parquet_dir / "payments.parquet").metadata.num_rows
    assert row_counts(url)["payments"] == full


def test_init_twice_changes_nothing(
    lending_database: URL,
    empty_database: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAKNA_EMBEDDING_PROVIDER", "hash")  # no model in the test tier
    context = Context(
        data_dir=tmp_path / "generated",
        seed=DEFAULT_SEED,
        scale=0.05,
        database_url=plain(empty_database),
        lending_loader_url=plain(lending_database),
    )
    first = io.StringIO()
    assert run_steps(STEPS, context, first) == 0
    assert "generated" in first.getvalue()
    assert "init: sync-catalog: synced (datasets +9" in first.getvalue()
    assert "init: build-index: indexed with hash-trigram-256 (documents +" in first.getvalue()
    second = io.StringIO()
    assert run_steps(STEPS, context, second) == 0
    assert second.getvalue().splitlines() == [
        "init: generate-sample-data: unchanged (all nine files present)",
        "init: load-lending: unchanged",
        "init: migrate: unchanged",
        "init: sync-catalog: unchanged",
        "init: build-index: unchanged",
    ]
    # The second run wrote no `sync_run` row and opened no reference issue.
    with psycopg.connect(plain(empty_database)) as connection:
        runs = connection.execute("SELECT count(*) FROM sync_run").fetchone()
        open_issues = connection.execute(
            "SELECT count(*) FROM reference_issue WHERE closed_at IS NULL"
        ).fetchone()
    assert runs == (1,) and open_issues == (0,)
    with psycopg.connect(plain(empty_database)) as connection:
        indexed = connection.execute("SELECT count(*) FROM search_document").fetchone()
        statistics = connection.execute(
            "SELECT relname, n_tup_upd, n_tup_del FROM pg_stat_user_tables "
            "WHERE relname IN ('search_document', 'semantic_object', 'index_state')"
        ).fetchall()
    assert indexed is not None and indexed[0] > 150
    assert all(upd == 0 and dele == 0 for _, upd, dele in statistics), statistics
