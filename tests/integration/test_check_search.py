"""A dry run of `deploy/check_search.py` with a stand-in for the real model.

The script is the proof of WP-07 acceptance 8 and 9, and runs for real only in the CI stack job.
Here it runs against a scratch index, so that a mistake in the script is found before CI.
"""

from __future__ import annotations

from collections.abc import Iterator

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
