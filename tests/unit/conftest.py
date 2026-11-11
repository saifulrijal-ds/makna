"""Fixtures for the generator tests: data is generated once per session."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from data.generator import DEFAULT_SEED, generate, write_parquet

from .generator_support import connect


@pytest.fixture(scope="session")
def data_small(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The default seed at scale 0.05."""
    out = tmp_path_factory.mktemp("generated_small")
    write_parquet(generate(DEFAULT_SEED, 0.05), out)
    return out


@pytest.fixture(scope="session")
def data_full(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The default seed at scale 1.0 (the dataset the dev stack and the evals use)."""
    out = tmp_path_factory.mktemp("generated_full")
    write_parquet(generate(DEFAULT_SEED, 1.0), out)
    return out


@pytest.fixture(scope="session", params=["scale_0.05", "scale_1.0"])
def dataset(request: pytest.FixtureRequest) -> Path:
    """Each test that takes this fixture runs on the small and on the full dataset."""
    name = "data_small" if request.param == "scale_0.05" else "data_full"
    path: Path = request.getfixturevalue(name)
    return path


@pytest.fixture(scope="session")
def db(dataset: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect(dataset)
    yield con
    con.close()


@pytest.fixture(scope="session")
def lending_db(data_small: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """The scale 0.05 sample data in DuckDB as `lending.core.<table>` (compiler tests)."""
    from .duckdb_support import load_lending

    con = load_lending(data_small)
    yield con
    con.close()
