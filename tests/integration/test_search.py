"""WP-07 acceptance 2, 3, 4, 6 and 10 on the sample index (`hash` embedding provider)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from makna.catalog.file import FileConnector
from makna.catalog.sync import sync_catalog
from makna.definitions.loader import definition_objects, load_definitions
from makna.index import indexer
from makna.model.definitions import Metric, parse_definition
from makna.model.objects import DefinitionObject
from makna.model.search import SearchSettings
from makna.search.embedding import HashProvider
from makna.search.hybrid import HybridSearch, SearchError

from ..catalog_support import DEFINITIONS, TAG_MAP, export_data, sample_objects, write_export


def searcher(engine: Engine, settings: SearchSettings | None = None) -> HybridSearch:
    return HybridSearch(engine, HashProvider(), settings)


@pytest.mark.parametrize("query", ["NPF", "kredit macet", "non-performing financing ratio"])
def test_synonyms_in_both_languages_find_the_npf_ratio(indexed_engine: Engine, query: str) -> None:
    result = searcher(indexed_engine).search(query, role="agent", limit=3)
    assert "metric:npf_ratio" in result.ids, result.ids


def test_a_deprecated_metric_is_hidden_unless_asked_for(indexed_engine: Engine) -> None:
    search = searcher(indexed_engine)
    query = "NPL ratio retired"
    assert "metric:npl_ratio" not in search.search(query, role="curator", limit=10).ids
    shown = search.search(query, role="agent", limit=10, include_deprecated=True)
    assert "metric:npl_ratio" in shown.ids


@pytest.mark.parametrize("role", ["agent", "viewer"])
def test_a_draft_is_hidden_from_agents_and_viewers(indexed_engine: Engine, role: str) -> None:
    result = searcher(indexed_engine).search("hapus buku write-off", role=role, limit=10)
    assert "metric:write_off_amount" not in result.ids
    assert all(hit.status == "approved" for hit in result.hits)


def test_a_curator_sees_the_draft(indexed_engine: Engine) -> None:
    result = searcher(indexed_engine).search("hapus buku write-off", role="curator", limit=10)
    assert "metric:write_off_amount" in result.ids


def twin(name: str, status: str) -> DefinitionObject:
    """A metric whose text is the same as its twin's except the name."""
    document = {
        "apiVersion": "makna/v1",
        "kind": "metric",
        "name": name,
        "label": "Kalibrasi Gamma",
        "description": "Metrik fiktif untuk menguji urutan status pada hasil pencarian.",
        "domain": "lending",
        "owner": {"team": "test"},
        "status": status,
        "sensitivity": "internal",
        "synonyms": {"en": ["gamma calibration"], "id": ["kalibrasi gamma"]},
        "type": "simple",
        "measure": "contract.disbursed_principal",
        "format": {"kind": "number"},
    }
    parsed = parse_definition(document)
    assert isinstance(parsed, Metric)
    return DefinitionObject(
        official=parsed if status == "approved" else None,
        proposal=parsed if status != "approved" else None,
    )


def test_a_draft_ranks_below_an_approved_metric_that_matches_equally(
    fresh_indexed_engine: Engine,
) -> None:
    # The draft is indexed first, so it wins every tie in both ranked lists; only the status
    # weight can put the approved twin ahead.
    for obj in (twin("gamma_draft", "draft"), twin("gamma_final", "approved")):
        indexer.index_object(fresh_indexed_engine, HashProvider(), obj)
    result = searcher(fresh_indexed_engine).search("kalibrasi gamma", role="curator", limit=5)
    assert result.ids[:2] == ["metric:gamma_final", "metric:gamma_draft"], result.ids
    first, second = result.hits[:2]
    assert (first.keyword_rank, second.keyword_rank) == (2, 1)
    assert (first.vector_rank, second.vector_rank) == (2, 1)


def test_promoting_a_draft_makes_it_findable_by_agents(fresh_indexed_engine: Engine) -> None:
    """Same text, only the status changes: `index_object` must still rewrite the document."""
    indexer.index_object(fresh_indexed_engine, HashProvider(), twin("gamma_promo", "draft"))
    search = searcher(fresh_indexed_engine)
    assert "metric:gamma_promo" not in search.search("kalibrasi gamma", role="agent").ids
    result = indexer.index_object(
        fresh_indexed_engine, HashProvider(), twin("gamma_promo", "approved")
    )
    assert result.documents_changed == 1
    with fresh_indexed_engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM search_document WHERE object_id = 'metric:gamma_promo'")
        ).scalar_one()
    assert status == "approved"
    assert "metric:gamma_promo" in search.search("kalibrasi gamma", role="agent").ids


def test_a_column_without_a_catalog_row_is_hidden_from_viewers_and_agents(
    fresh_indexed_engine: Engine,
) -> None:
    """Fail closed: the document outlives its column until the next build."""
    column = "column:lending.core.customers.city"
    with fresh_indexed_engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM catalog_column WHERE name = 'city' AND dataset_id IN "
                "(SELECT id FROM catalog_dataset WHERE engine_fqn = 'lending.core.customers')"
            )
        )
    search = searcher(fresh_indexed_engine)
    for role in ("viewer", "agent"):
        assert (
            column not in search.search("customer city", role=role, kinds=["column"], limit=10).ids
        )
    assert column in search.search("customer city", role="curator", kinds=["column"], limit=10).ids
    assert (
        "column:lending.core.customers.province"
        in search.search("customer province", role="agent", kinds=["column"], limit=10).ids
    )


def flat_settings(**weights: float) -> SearchSettings:
    kinds = ["metric", "entity", "dimension", "rule", "glossary_term", "domain", "column"]
    statuses = ["approved", "deprecated", "draft", "in_review"]
    return SearchSettings(
        status_weights=dict.fromkeys(statuses, 1.0),
        kind_weights=dict.fromkeys(kinds, 1.0),
        **weights,
    )


FUSION_QUERY = "kredit macet rasio per wilayah"
FUSION_TSQUERY = "'kredit':* | 'macet':* | 'rasio':* | 'wilayah':*"


def test_with_no_keyword_weight_the_order_is_the_vector_order(indexed_engine: Engine) -> None:
    result = searcher(indexed_engine, flat_settings(w_keyword=0.0)).search(
        FUSION_QUERY, role="curator", limit=10
    )
    vector = "[" + ",".join(map(str, HashProvider().embed_query(FUSION_QUERY))) + "]"
    with indexed_engine.connect() as connection:
        expected = connection.execute(
            text(
                "SELECT object_id FROM search_document WHERE kind <> 'column' "
                "AND status IN ('approved', 'draft', 'in_review') "
                "ORDER BY embedding <=> CAST(:v AS vector), id LIMIT 10"
            ),
            {"v": vector},
        ).scalars()
        assert result.ids == list(expected)
    assert len(result.ids) == 10


def test_with_no_vector_weight_the_order_is_the_keyword_order(indexed_engine: Engine) -> None:
    result = searcher(indexed_engine, flat_settings(w_vector=0.0)).search(
        FUSION_QUERY, role="curator", limit=10
    )
    with indexed_engine.connect() as connection:
        expected = connection.execute(
            text(
                "SELECT object_id FROM search_document, to_tsquery('simple', :q) AS tsq "
                "WHERE kind <> 'column' AND status IN ('approved', 'draft', 'in_review') "
                "AND tsv @@ tsq ORDER BY ts_rank_cd(tsv, tsq) DESC, id LIMIT 10"
            ),
            {"q": FUSION_TSQUERY},
        ).scalars()
        assert result.ids == list(expected)
    assert len(result.ids) == 10


@pytest.mark.parametrize(
    "query",
    [
        "kredit macet",
        "Berapa total pencairan per wilayah tahun ini?",
        "Total pencairan tahun ini untuk tiap wilayah?",
        "NPF ratio by region last quarter",
    ],
)
def test_confidence_is_not_low_for_valid_questions(indexed_engine: Engine, query: str) -> None:
    confidence = searcher(indexed_engine).search(query, role="agent").confidence
    assert confidence.band != "low", confidence
    assert confidence.guidance is None


@pytest.mark.parametrize(
    "query", ["website conversion funnel", "employee attrition by region last month"]
)
def test_confidence_is_low_when_nothing_matches(indexed_engine: Engine, query: str) -> None:
    confidence = searcher(indexed_engine).search(query, role="agent").confidence
    assert confidence.band == "low", confidence
    assert confidence.guidance and "do not approximate" in confidence.guidance


def test_qualifier_words_are_left_out_of_coverage(indexed_engine: Engine) -> None:
    confidence = (
        searcher(indexed_engine).search("NPF ratio by region last quarter", role="agent").confidence
    )
    assert set(confidence.content_words) == {"npf", "ratio"}
    assert {"region", "last", "quarter"} <= set(confidence.qualifier_words)


NATIONAL_ID = "column:lending.core.customers.national_id"


@pytest.mark.parametrize("role", ["viewer", "agent"])
def test_a_personal_column_is_never_returned_to_a_viewer_or_agent(
    indexed_engine: Engine, role: str
) -> None:
    search = searcher(indexed_engine)
    for kinds in (["column"], None):
        result = search.search("national id nomor identitas", role=role, kinds=kinds, limit=10)
        assert NATIONAL_ID not in result.ids
    other = search.search("customer city", role=role, kinds=["column"], limit=10)
    assert "column:lending.core.customers.city" in other.ids


@pytest.mark.parametrize("role", ["curator", "approver", "admin"])
def test_a_personal_column_is_returned_to_a_curator(indexed_engine: Engine, role: str) -> None:
    result = searcher(indexed_engine).search("national id", role=role, kinds=["column"], limit=10)
    assert NATIONAL_ID in result.ids


def test_an_unknown_role_or_kind_is_an_error(indexed_engine: Engine) -> None:
    with pytest.raises(SearchError, match="role"):
        searcher(indexed_engine).search("npf", role="stranger")
    with pytest.raises(SearchError, match="kind"):
        searcher(indexed_engine).search("npf", role="agent", kinds=["table"])


def test_a_search_fails_clearly_when_the_index_was_built_with_another_model(
    indexed_engine: Engine,
) -> None:
    other = HybridSearch(indexed_engine, HashProvider(128))
    with pytest.raises(SearchError, match="makna index build"):
        other.search("npf", role="agent")


def test_columns_of_a_removed_dataset_are_not_indexed(
    fresh_indexed_engine: Engine, tmp_path: Path
) -> None:
    data = export_data()
    data["datasets"] = [d for d in data["datasets"] if d["name"] != "branches"]
    sync_catalog(
        fresh_indexed_engine,
        FileConnector(write_export(tmp_path / "c.yaml", data)),
        tag_map=TAG_MAP,
    )
    with fresh_indexed_engine.connect() as connection:
        removed = connection.execute(
            text("SELECT count(*) FROM catalog_dataset WHERE removed_at IS NOT NULL")
        ).scalar_one()
    assert removed == 1
    objects = definition_objects(load_definitions(DEFINITIONS))
    result = indexer.build(fresh_indexed_engine, HashProvider(), objects)
    assert result.documents_removed == 8  # the 8 columns of lending.core.branches
    with fresh_indexed_engine.connect() as connection:
        left = connection.execute(
            text("SELECT count(*) FROM search_document WHERE object_id LIKE 'column:%branches.%'")
        ).scalar_one()
    assert left == 0
    assert sample_objects()  # the definitions themselves are untouched
