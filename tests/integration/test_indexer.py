"""WP-07 acceptance 1 and 7: what the indexer writes, and that it writes only differences."""

from __future__ import annotations

from collections import Counter

import yaml
from sqlalchemy import Engine, func, select

from makna.definitions.loader import definition_objects, load_definitions
from makna.index import indexer
from makna.index.tables import (
    index_state,
    search_document,
    semantic_object,
)
from makna.model.definitions import Metric, parse_definition
from makna.model.objects import DefinitionObject
from makna.search.embedding import HashProvider

from ..catalog_support import DEFINITIONS, export_data


def kinds(engine: Engine) -> Counter[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(search_document.c.kind, func.count()).group_by(search_document.c.kind)
        )
        return Counter({kind: count for kind, count in rows})


def sample_objects() -> list[DefinitionObject]:
    return definition_objects(load_definitions(DEFINITIONS))


def expected_counts() -> Counter[str]:
    """What section 6.4 says becomes a document, counted straight from the YAML files."""
    counts: Counter[str] = Counter()
    for path in sorted(DEFINITIONS.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        counts[data["kind"]] += 1
        counts["dimension"] += len(data.get("dimensions", []))
        counts["rule"] += len(data.get("rules", []))
    return counts


def test_one_document_for_each_case_of_section_6_4(indexed_engine: Engine) -> None:
    found = kinds(indexed_engine)
    expected = expected_counts()
    export = export_data()
    columns = sum(len(d["columns"]) for d in export["datasets"])
    catalog_terms = len(export["glossary_terms"])
    # Fixed numbers of the sample domain, so that a change to the samples is noticed here:
    # 24 approved or deprecated metrics and 1 draft proposal.
    assert (expected["metric"], expected["entity"]) == (25, 9)
    assert (expected["glossary_term"], expected["domain"]) == (8, 1)
    assert (columns, catalog_terms) == (92, 8)
    assert found["metric"] == expected["metric"]
    assert found["entity"] == expected["entity"]
    assert found["domain"] == expected["domain"]
    assert found["dimension"] == expected["dimension"] > 0
    assert found["rule"] == expected["rule"] > 0
    assert found["column"] == columns
    # the 8 Makna glossary terms (none claims a catalog term) plus the 8 catalog terms
    assert found["glossary_term"] == expected["glossary_term"] + catalog_terms
    assert sum(found.values()) == sum(expected.values()) + columns + catalog_terms


def test_statuses_follow_the_lifecycle(indexed_engine: Engine) -> None:
    with indexed_engine.connect() as connection:
        status = dict(
            connection.execute(
                select(search_document.c.object_id, search_document.c.status).where(
                    search_document.c.object_id.in_(
                        ["metric:npf_ratio", "metric:npl_ratio", "metric:write_off_amount"]
                    )
                )
            ).all()
        )
    assert status == {
        "metric:npf_ratio": "approved",
        "metric:npl_ratio": "deprecated",
        "metric:write_off_amount": "draft",
    }


def test_the_metric_row_carries_inputs_and_the_formula_line(indexed_engine: Engine) -> None:
    with indexed_engine.connect() as connection:
        derived = dict(
            connection.execute(
                select(semantic_object.c.id, semantic_object.c.derived).where(
                    semantic_object.c.id.in_(["metric:npf_ratio", "metric:disbursement_amount"])
                )
            ).all()
        )
    assert derived["metric:npf_ratio"] == {
        "inputs": ["metric:npf_outstanding_principal", "metric:outstanding_principal"],
        "formula": "NPF Outstanding Principal divided by Outstanding Principal",
    }
    assert derived["metric:disbursement_amount"]["inputs"] == [
        "measure:contract.disbursed_principal"
    ]


def test_the_embedding_model_is_recorded(indexed_engine: Engine) -> None:
    with indexed_engine.connect() as connection:
        state = dict(connection.execute(select(index_state.c.key, index_state.c.value)).all())
    assert state["embedding_model"] == HashProvider().model_id
    assert state["definitions_version"]


def counters(engine: Engine) -> dict[str, tuple[int, int, int]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(search_document.c.object_id, search_document.c.text_hash)
        ).all()
        updated = connection.execute(
            select(semantic_object.c.id, semantic_object.c.updated_at)
        ).all()
    return {"documents": tuple(sorted(rows)), "objects": tuple(sorted(updated))}  # type: ignore[dict-item]


def test_a_second_build_changes_nothing(fresh_indexed_engine: Engine) -> None:
    before = counters(fresh_indexed_engine)
    result = indexer.build(fresh_indexed_engine, HashProvider(), sample_objects())
    assert result.total_changes == 0
    assert result.documents_embedded == 0
    assert counters(fresh_indexed_engine) == before


def with_extra_synonym(obj: DefinitionObject, word: str) -> DefinitionObject:
    document = obj.served.model_dump(mode="json", exclude_unset=True, by_alias=True)
    document["synonyms"]["en"].append(word)
    changed = parse_definition(document)
    assert isinstance(changed, Metric)
    return DefinitionObject(official=changed, official_path=obj.official_path)


def test_index_object_changes_only_that_objects_rows(fresh_indexed_engine: Engine) -> None:
    objects = sample_objects()
    target = next(o for o in objects if o.id == "metric:npf_ratio")
    before = counters(fresh_indexed_engine)
    result = indexer.index_object(
        fresh_indexed_engine,
        HashProvider(),
        with_extra_synonym(target, "bad loans share"),
        objects,
    )
    after = counters(fresh_indexed_engine)
    assert result.documents_changed == 1 and result.objects_changed == 1
    assert result.documents_embedded == 1
    changed_documents = set(after["documents"]) - set(before["documents"])  # type: ignore[call-overload]
    assert {oid for oid, _ in changed_documents} == {"metric:npf_ratio"}
    changed_objects = set(after["objects"]) - set(before["objects"])  # type: ignore[call-overload]
    assert {oid for oid, _ in changed_objects} == {"metric:npf_ratio"}
    assert len(after["documents"]) == len(before["documents"])  # type: ignore[arg-type]


def test_index_object_with_no_change_writes_nothing(fresh_indexed_engine: Engine) -> None:
    objects = sample_objects()
    target = next(o for o in objects if o.id == "entity:branch")
    result = indexer.index_object(fresh_indexed_engine, HashProvider(), target, objects)
    assert result.total_changes == 0


def test_removing_a_dimension_removes_its_document(fresh_indexed_engine: Engine) -> None:
    objects = sample_objects()
    target = next(o for o in objects if o.id == "entity:branch")
    document = target.served.model_dump(mode="json", exclude_unset=True, by_alias=True)
    document["dimensions"] = [d for d in document["dimensions"] if d["name"] != "province"]
    changed = parse_definition(document)
    result = indexer.index_object(
        fresh_indexed_engine,
        HashProvider(),
        DefinitionObject(official=changed),
        objects,
    )
    assert result.documents_removed == 1
    with fresh_indexed_engine.connect() as connection:
        ids = connection.execute(
            select(search_document.c.object_id).where(
                search_document.c.object_id == "dimension:branch.province"
            )
        ).all()
    assert ids == []
