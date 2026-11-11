"""WP-06 acceptance 1 to 6 against PostgreSQL: sync, removal and restore, issues R01 to R04."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select

from makna.catalog.base import ConnectorError
from makna.catalog.file import FileConnector
from makna.catalog.references import open_issues
from makna.catalog.sync import sync_catalog
from makna.index.tables import (
    catalog_column,
    catalog_dataset,
    catalog_glossary_term,
    catalog_source,
    reference_issue,
    sync_run,
)

from ..catalog_support import (
    EXPORT,
    TAG_MAP,
    dimension,
    export_data,
    export_without_column,
    make_entity,
    make_term,
    sample_objects,
    write_export,
)
from ..unit.generator_support import doc_columns


def sync(engine: Engine, path: Path = EXPORT, **kwargs: Any) -> dict[str, Any]:
    result = sync_catalog(engine, FileConnector(path), tag_map=TAG_MAP, **kwargs)
    return result.summary.as_dict()


def count(engine: Engine, table: Any) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def issues(engine: Engine, *, closed: bool = False) -> list[tuple[str, str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(reference_issue).where(
                reference_issue.c.closed_at.is_not(None)
                if closed
                else reference_issue.c.closed_at.is_(None)
            )
        )
        return sorted((r.code, r.object_id, r.part) for r in rows)


def last_summary(engine: Engine) -> dict[str, Any]:
    with engine.connect() as connection:
        summary: dict[str, Any] = connection.execute(
            select(sync_run.c.summary).order_by(sync_run.c.id.desc()).limit(1)
        ).scalar_one()
        return summary


# --- Acceptance 1 --------------------------------------------------------------------------


def test_syncing_the_sample_export_creates_nine_datasets_and_the_columns_of_section_3(
    migrated_engine: Engine,
) -> None:
    summary = sync(migrated_engine)
    expected_columns = sum(len(c) for c in doc_columns().values())
    assert count(migrated_engine, catalog_dataset) == 9
    assert count(migrated_engine, catalog_column) == expected_columns
    assert count(migrated_engine, catalog_glossary_term) == 8
    assert summary["datasets"]["added"] == 9
    assert summary["columns"]["added"] == expected_columns
    assert last_summary(migrated_engine) == summary
    with migrated_engine.connect() as connection:
        row = connection.execute(select(catalog_source)).one()
    assert (row.name, row.connector) == ("lending-sample", "file") and row.last_sync_at


def test_a_second_sync_reports_zero_changes(migrated_engine: Engine) -> None:
    sync(migrated_engine)
    second = sync(migrated_engine)
    assert second["total_changes"] == 0
    for group in ("datasets", "columns", "glossary_terms"):
        assert (second[group]["added"], second[group]["changed"], second[group]["removed"]) == (
            0,
            0,
            0,
        )
    assert second["datasets"]["unchanged"] == 9
    assert second["reference_issues"] == {"opened": 0, "closed": 0}
    assert last_summary(migrated_engine) == second  # recorded in sync_run.summary
    assert count(migrated_engine, sync_run) == 2


def test_an_unchanged_sync_can_write_nothing(migrated_engine: Engine) -> None:
    sync(migrated_engine)
    result = sync_catalog(
        migrated_engine, FileConnector(EXPORT), tag_map=TAG_MAP, record_unchanged=False
    )
    assert result.run_id is None and result.summary.total_changes == 0
    assert count(migrated_engine, sync_run) == 1


def test_changes_to_a_dataset_a_column_and_a_term_are_counted(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    sync(migrated_engine)
    data = copy.deepcopy(export_data())
    data["datasets"][0]["description"] = "Branches, reworded."
    data["datasets"][0]["columns"][1]["description"] = "Reworded."
    data["glossary_terms"][0]["definition"] = "Reworded."
    data["glossary_terms"].append(
        {"catalog_id": "glossary.new", "name": "New", "definition": "A new term."}
    )
    summary = sync(migrated_engine, write_export(tmp_path / "e.yaml", data))
    assert summary["datasets"]["changed"] == 1 and summary["datasets"]["unchanged"] == 8
    assert summary["columns"] == {"added": 0, "changed": 1, "removed": 0}
    assert summary["glossary_terms"] == {"added": 1, "changed": 1, "removed": 0}


def test_a_table_missing_from_the_export_is_marked_removed_and_comes_back(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    sync(migrated_engine)
    data = copy.deepcopy(export_data())
    data["datasets"] = [d for d in data["datasets"] if d["name"] != "products"]
    summary = sync(migrated_engine, write_export(tmp_path / "e.yaml", data))
    assert summary["datasets"]["removed"] == 1
    with migrated_engine.connect() as connection:
        removed = connection.execute(
            select(catalog_dataset.c.engine_fqn).where(catalog_dataset.c.removed_at.is_not(None))
        ).scalars()
        assert list(removed) == ["lending.core.products"]
    back = sync(migrated_engine)
    assert back["datasets"]["added"] == 1 and back["datasets"]["removed"] == 0
    with migrated_engine.connect() as connection:
        assert not list(
            connection.execute(
                select(catalog_dataset).where(catalog_dataset.c.removed_at.is_not(None))
            )
        )


# --- Acceptance 4: derived sensitivity ---------------------------------------------------------


def test_a_pii_tagged_column_gets_derived_sensitivity_personal(migrated_engine: Engine) -> None:
    sync(migrated_engine)
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            select(
                catalog_dataset.c.name, catalog_column.c.name, catalog_column.c.derived_sensitivity
            )
            .select_from(catalog_column.join(catalog_dataset))
            .where(catalog_column.c.derived_sensitivity.is_not(None))
        ).all()
    assert sorted((t, c, d) for t, c, d in rows) == [
        ("customers", name, "personal")
        for name in ("birth_date", "email", "full_name", "national_id", "phone_number")
    ]


def test_changing_the_tag_map_tightens_at_the_next_sync(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    data = copy.deepcopy(export_data())
    data["datasets"][0]["columns"][0]["tags"] = ["Tier.Gold"]
    edited = write_export(tmp_path / "e.yaml", data)
    sync_catalog(migrated_engine, FileConnector(edited), tag_map=TAG_MAP)
    stricter = {**TAG_MAP, "Tier.Gold": "confidential"}
    summary = sync_catalog(migrated_engine, FileConnector(edited), tag_map=stricter).summary
    assert summary.columns.changed == 1
    with migrated_engine.connect() as connection:
        level = connection.execute(
            select(catalog_column.c.derived_sensitivity).where(catalog_column.c.name == "branch_id")
        ).scalar_one()
    assert level == "confidential"


# --- Acceptance 2: removing a column opens R02 for each part that reads it; restoring closes -----


def test_removing_a_column_opens_r02_for_each_part_that_reads_it_and_restoring_closes_them(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    entities, terms = sample_objects()
    first = sync(migrated_engine, entities=entities, glossary_terms=terms)
    assert first["reference_issues"] == {"opened": 0, "closed": 0}

    # `sts_putusan` is read by the decision_status dimension and by two measure filters.
    reduced = export_without_column(tmp_path / "reduced.yaml", "loan_applications", "sts_putusan")
    removed = sync(migrated_engine, reduced, entities=entities, glossary_terms=terms)
    assert removed["columns"] == {"added": 0, "changed": 0, "removed": 1}
    assert last_summary(migrated_engine)["columns"]["removed"] == 1

    reading_parts = sorted(
        (f"entity:{e.name}", f"dimension:{d.name}")
        for e in entities
        if e.source.table == "lending.core.loan_applications"
        for d in e.dimensions
        if "sts_putusan" in (d.expr or d.name).lower()
    ) + sorted(
        (f"entity:{e.name}", f"measure:{m.name}")
        for e in entities
        if e.source.table == "lending.core.loan_applications"
        for m in e.measures or []
        if "sts_putusan" in ((m.expr or "") + (m.filter or "")).lower()
    )
    assert len(reading_parts) >= 3
    assert issues(migrated_engine) == sorted(("R02", o, p) for o, p in reading_parts)
    assert removed["reference_issues"] == {"opened": len(reading_parts), "closed": 0}

    restored = sync(migrated_engine, EXPORT, entities=entities, glossary_terms=terms)
    assert restored["columns"]["added"] == 1
    assert restored["reference_issues"] == {"opened": 0, "closed": len(reading_parts)}
    assert issues(migrated_engine) == []
    assert issues(migrated_engine, closed=True) == sorted(("R02", o, p) for o, p in reading_parts)
    with migrated_engine.connect() as connection:
        assert not list(
            connection.execute(select(reference_issue).where(reference_issue.c.closed_at.is_(None)))
        )


def test_a_still_true_issue_is_not_reopened_or_duplicated(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    entities, terms = sample_objects()
    sync(migrated_engine, entities=entities, glossary_terms=terms)
    reduced = export_without_column(tmp_path / "reduced.yaml", "customers", "city")
    sync(migrated_engine, reduced, entities=entities, glossary_terms=terms)
    before = issues(migrated_engine)
    again = sync(migrated_engine, reduced, entities=entities, glossary_terms=terms)
    assert again["reference_issues"] == {"opened": 0, "closed": 0}
    assert issues(migrated_engine) == before and before


# --- Acceptance 3: each of R01 to R04 opens exactly that issue -----------------------------------


def test_r01_opens_exactly_r01(migrated_engine: Engine) -> None:
    entity = make_entity(table="lending.core.nowhere", dimensions=[dimension("city")])
    sync(migrated_engine, entities=[entity], glossary_terms=[])
    assert issues(migrated_engine) == [("R01", "entity:thing", "source.table")]


def test_r02_opens_exactly_r02(migrated_engine: Engine) -> None:
    entity = make_entity(
        table="lending.core.branches",
        primary_key=("branch_id",),
        dimensions=[dimension("city", "no_such_column")],
    )
    sync(migrated_engine, entities=[entity], glossary_terms=[])
    assert issues(migrated_engine) == [("R02", "entity:thing", "dimension:city")]


def test_r03_opens_exactly_r03_and_a_dimension_declared_personal_is_fine(
    migrated_engine: Engine,
) -> None:
    internal = make_entity(
        table="lending.core.customers",
        primary_key=("customer_id",),
        dimensions=[dimension("mail", "email", sensitivity="internal")],
    )
    sync(migrated_engine, entities=[internal], glossary_terms=[])
    assert issues(migrated_engine) == [("R03", "entity:thing", "dimension:mail")]
    personal = make_entity(
        table="lending.core.customers",
        primary_key=("customer_id",),
        dimensions=[
            dimension("mail", "email", sensitivity="personal", masking={"method": "redact"})
        ],
    )
    summary = sync(migrated_engine, entities=[personal], glossary_terms=[])
    assert issues(migrated_engine) == []
    assert summary["reference_issues"] == {"opened": 0, "closed": 1}


def test_r04_opens_exactly_r04(migrated_engine: Engine) -> None:
    terms = [make_term("npf", "glossary.npf"), make_term("nothing", "glossary.nothing")]
    sync(migrated_engine, entities=[], glossary_terms=terms)
    assert issues(migrated_engine) == [("R04", "glossary_term:nothing", "catalog_term")]


# --- Acceptance 6: sample definitions and sample export give zero open issues ------------------


def test_the_sample_definitions_and_the_sample_export_have_no_open_reference_issue(
    migrated_engine: Engine,
) -> None:
    entities, terms = sample_objects()
    assert len(entities) == 9 and len(terms) == 8
    summary = sync(migrated_engine, entities=entities, glossary_terms=terms)
    assert summary["reference_issues"] == {"opened": 0, "closed": 0}
    assert issues(migrated_engine) == []
    assert count(migrated_engine, reference_issue) == 0
    with migrated_engine.connect() as connection:
        assert open_issues(connection) == []


# --- Acceptance 5 (database side): a rejected file leaves the database untouched -----------------


def test_a_file_that_does_not_match_the_format_is_rejected_and_nothing_is_written(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    sync(migrated_engine)
    data = copy.deepcopy(export_data())
    data["datasets"][3]["columns"][2]["data_type"] = "varchar"
    bad = write_export(tmp_path / "bad.yaml", data)
    with pytest.raises(ConnectorError, match=r"datasets\[3\]\.columns\[2\]\.data_type"):
        sync(migrated_engine, bad)
    assert count(migrated_engine, sync_run) == 1
