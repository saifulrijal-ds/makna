"""A dry run of `deploy/check_search.py` with a stand-in for the real model.

The script is the proof of WP-07 acceptance 8 and 9, and runs for real only in the CI stack job.
Here it runs against a scratch index, so that a mistake in the script is found before CI.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from deploy import check_search
from makna.model.search import DEFAULT_EMBEDDING_MODEL
from makna.search.embedding import HashProvider

from .conftest import _build_sample_index


class StandIn(HashProvider):
    """Hashed trigrams with the identity and the size of the real default model."""

    def __init__(self) -> None:
        super().__init__(384)
        self.model_id = DEFAULT_EMBEDDING_MODEL


@pytest.fixture
def stand_in_index(empty_database: URL, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _build_sample_index(empty_database, StandIn())
    monkeypatch.setenv("MAKNA_DATABASE_URL", empty_database.render_as_string(hide_password=False))
    monkeypatch.setattr(check_search, "make_real_provider", StandIn)
    check_search.suite.cache_clear()
    yield
    check_search.suite.cache_clear()


def test_the_checks_pass_on_a_good_index(
    stand_in_index: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = check_search.main([])
    out = capsys.readouterr().out
    assert code == 0, out
    for name in (
        "check_latency",
        "check_real_model",
        "check_index_builds_with_the_real_model",
        "check_pembiayaan_bermasalah",
        "report_legs",
    ):
        assert f"ok   {name}" in out
    assert "200 searches" in out and "recall@3" in out


def test_a_wrong_dimension_fails_the_real_model_check(
    stand_in_index: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Small(StandIn):
        def __init__(self) -> None:
            super().__init__()
            self.dimension = 256

    monkeypatch.setattr(check_search, "make_real_provider", Small)
    check_search.suite.cache_clear()
    assert check_search.main([]) == 1
    assert "FAIL check_real_model" in capsys.readouterr().out


def test_the_latency_budget_is_enforced(
    stand_in_index: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check_search, "P95_BUDGET_MS", 0.0)
    assert check_search.main([]) == 1
    assert "FAIL check_latency" in capsys.readouterr().out


def test_the_index_check_keeps_the_available_dimensions_of_the_index(
    stand_in_index: None, empty_database: URL
) -> None:
    """The check re-runs the index build; with no derive hook it would drop the facts that the
    compile evals read (part B), and every later step of the stack job would find none."""
    from sqlalchemy import create_engine, text

    from makna.definitions.loader import definition_objects, load_definitions
    from makna.index import indexer
    from makna.model.governance import STRICT
    from makna.services.derive import make_derive
    from tests.catalog_support import DEFINITIONS

    engine = create_engine(empty_database)
    count = text("SELECT count(*) FROM semantic_object WHERE derived ? 'dimensions'")
    try:
        objects = definition_objects(load_definitions(DEFINITIONS))
        indexer.build(engine, StandIn(), objects, derive=make_derive(STRICT))
        with engine.connect() as connection:
            before = connection.execute(count).scalar_one()
        assert before > 0
        check_search.check_index_builds_with_the_real_model()
        with engine.connect() as connection:
            assert connection.execute(count).scalar_one() == before
    finally:
        engine.dispose()


def test_the_index_check_refuses_definitions_that_fail_validation(
    stand_in_index: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M-3: `check_search` builds the index too, so it passes the gate of `index build`."""
    import shutil

    from tests.catalog_support import DEFINITIONS

    broken = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, broken)
    path = broken / "lending" / "entities" / "contract.yaml"
    path.write_text(
        path.read_text().replace("disbursement_date IS NOT NULL", "disbursement_date ^ 2", 1)
    )
    monkeypatch.setattr(check_search, "DEFINITIONS", broken)
    with pytest.raises(AssertionError, match="V05"):
        check_search.check_index_builds_with_the_real_model()
