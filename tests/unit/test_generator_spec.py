"""DAT-1 acceptance 2: the nine tables have exactly the columns and types of section 3."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from data.generator import TABLES, TABLES_BY_NAME, arrow_schema, get_table, plan

from .generator_support import (
    TABLE_NAMES,
    connect,
    doc_columns,
    doc_relationships,
    doc_target_sizes,
)

PERSONAL = {"full_name", "national_id", "phone_number", "email", "birth_date"}


def test_specification_lists_the_nine_tables_of_the_document() -> None:
    assert tuple(t.name for t in TABLES) == TABLE_NAMES
    assert set(doc_columns()) == set(TABLE_NAMES)
    assert set(TABLES_BY_NAME) == set(TABLE_NAMES)


@pytest.mark.parametrize("table", TABLE_NAMES)
def test_specification_matches_section_3_columns_and_types(table: str) -> None:
    spec = get_table(table)
    assert [(c.name, c.sql_type) for c in spec.columns] == doc_columns()[table]


@pytest.mark.parametrize("table", TABLE_NAMES)
def test_arrow_schema_has_one_field_per_column(table: str) -> None:
    spec = get_table(table)
    assert arrow_schema(spec).names == list(spec.column_names)


def test_relationships_match_section_3_10() -> None:
    from_spec = {
        (t.name, fk.column, fk.ref_table, fk.relationship) for t in TABLES for fk in t.foreign_keys
    }
    assert from_spec == doc_relationships()


def test_primary_keys_are_columns_of_their_table() -> None:
    for spec in TABLES:
        assert spec.primary_key
        assert set(spec.primary_key) <= set(spec.column_names)
    assert get_table("contract_monthly_snapshots").primary_key == ("snapshot_date", "contract_id")


def test_exactly_five_personal_columns_all_on_customers() -> None:
    flagged = {(t.name, c) for t in TABLES for c in t.personal_columns}
    assert flagged == {("customers", c) for c in PERSONAL}


def test_legacy_tables_carry_the_clean_semantic_names() -> None:
    assert get_table("loan_applications").column("sts_putusan").semantic_name == "decision_status"
    assert get_table("collection_actions").column("kontrak_id").semantic_name == "contract_id"
    named = [c for t in TABLES for c in t.columns if c.semantic_name]
    assert {t.name for t in TABLES if any(c.semantic_name for c in t.columns)} == {
        "loan_applications",
        "collection_actions",
    }
    assert len(named) == 12 + 8


@pytest.mark.parametrize("table", TABLE_NAMES)
def test_generated_files_have_exactly_the_specified_columns_and_types(
    data_small: Path, table: str
) -> None:
    con = connect(data_small)
    described = con.execute(f"DESCRIBE {table}").fetchall()
    assert [(row[0], str(row[1]).lower()) for row in described] == doc_columns()[table]


def test_planned_counts_at_scale_1_are_the_section_2_targets() -> None:
    targets = doc_target_sizes()
    sizes = plan(1.0)
    assert sizes.branches == targets["branches"]
    assert sizes.products == targets["products"]
    assert sizes.customers == targets["customers"]
    assert sizes.applications == targets["loan_applications"]


def test_row_counts_at_scale_1_are_within_25_percent_of_the_targets(data_full: Path) -> None:
    # This test uses the full-scale dataset (about 4 seconds, generated once per session).
    targets = doc_target_sizes()
    assert set(targets) == set(TABLE_NAMES)
    con = connect(data_full)
    for table, target in targets.items():
        rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert rows is not None
        assert abs(rows[0] - target) <= 0.25 * target, (table, rows[0], target)


def test_row_counts_scale_with_the_scale_factor(data_small: Path) -> None:
    con: duckdb.DuckDBPyConnection = connect(data_small)
    customers = con.execute("SELECT count(*) FROM customers").fetchone()
    applications = con.execute("SELECT count(*) FROM loan_applications").fetchone()
    assert customers is not None and applications is not None
    assert customers[0] == plan(0.05).customers == 300
    assert applications[0] == plan(0.05).applications == 450
