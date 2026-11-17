"""Milestone review "core complete": integration tests that prove findings of
docs/reviews/core-complete.md.

Marked `xfail(strict=True)`, as in `tests/unit/test_review_core_complete.py`: remove the marker
to see the failure; when the finding is fixed the marker must go.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, text

from makna.catalog.file import FileConnector
from makna.catalog.sync import sync_catalog
from makna.cli.main import main
from makna.config import load_search_runtime
from makna.search.embedding import HashProvider
from makna.search.hybrid import HybridSearch

from ..catalog_support import DEFINITIONS, EXPORT, TAG_MAP, sample_objects

BROKEN_METRIC = """\
apiVersion: makna/v1
kind: metric
name: misspelt_active_principal
label: Misspelt Active Principal
description: Principal of active contracts, with the status value misspelt in the filter.
domain: lending
owner: {team: credit-risk}
status: approved
sensitivity: internal
synonyms:
  en: [misspelt active principal]
  id: [pokok aktif salah eja]
version: 1
type: simple
measure: contract.disbursed_principal
filters:
  - {dimension: contract.contract_status, op: "=", value: actve}
format: {kind: currency, unit: IDR, decimals: 0}
example_questions:
  - How much principal do active contracts hold?
  - Berapa pokok kontrak aktif?
example_requests:
  - question: "Active principal by payment channel"
    request: {dimensions: [payment.payment_channel]}
"""


@pytest.fixture
def database(migrated_url: URL, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("MAKNA_DATABASE_URL", migrated_url.render_as_string(hide_password=False))
    monkeypatch.setenv("MAKNA_EMBEDDING_PROVIDER", "hash")
    entities, terms = sample_objects()
    sync_catalog(
        migrated_engine,
        FileConnector(EXPORT),
        tag_map=TAG_MAP,
        entities=entities,
        glossary_terms=terms,
    )
    return migrated_engine


@pytest.mark.integration
def test_index_build_does_not_serve_a_definition_that_fails_validation(
    database: Engine, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`makna validate` rejects this file twice: V10 (the filter value `actve` is not a value of
    `contract.contract_status`) and V19 (its example request cannot compile). `makna index build`
    must not make it an approved metric that agents find and compile. Before WP-15C it exited 0
    and served the metric as approved; its SQL filtered on `actve` and returned no amount."""
    definitions = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, definitions)
    (definitions / "lending" / "metrics" / "misspelt_active_principal.yaml").write_text(
        BROKEN_METRIC, encoding="utf-8"
    )
    assert main(["validate", str(definitions)]) == 1  # the file really is invalid
    code = main(["index", "build", "--definitions", str(definitions)])
    capsys.readouterr()
    with database.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM semantic_object WHERE id = :id"),
            {"id": "metric:misspelt_active_principal"},
        ).scalar_one_or_none()
    assert code == 1 and status is None, f"exit {code}, served with status {status}"


@pytest.mark.integration
def test_a_search_for_a_dimension_returns_it_in_the_first_five(
    database: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """`makna_search` says it finds dimensions, and its default limit is 5. "region" is the name
    of `branch.region_name` and matches it best in the keyword list. With the configured weights
    it must be among the first five hits. Before WP-15D six metrics that only mention a region in
    an example question came first, because the kind weight 0.8 outweighed any difference of
    rank."""
    assert main(["index", "build", "--definitions", str(DEFINITIONS)]) == 0
    capsys.readouterr()
    settings = load_search_runtime()
    provider = HashProvider(settings.embedding.hash.dimension)
    search = HybridSearch(database, provider, settings.search)
    ids = search.search("region", role="viewer", limit=5).ids
    assert "dimension:branch.region_name" in ids, ids
