"""DAT-1 acceptance 6: catalog descriptions equal section 6.1 exactly (Q11, D-14)."""

from __future__ import annotations

import re

import pytest

from data.generator import TABLES, TableSpec, get_table

from .generator_support import doc_catalog_exceptions, doc_columns

# Words that mark a description as Indonesian (the descriptions in section 6.1 that are).
INDONESIAN_WORDS = {
    "tanggal", "tgl", "cabang", "bulanan", "kontrak", "penghasilan", "pencairan", "bulan",
    "dana", "jenis", "hasil", "nominal", "janji", "aksi", "kol",
}  # fmt: skip


def test_section_6_1_lists_the_expected_objects() -> None:
    exceptions = doc_catalog_exceptions()
    assert exceptions[("loan_applications", None)] == "LOS extract"
    assert exceptions[("installment_schedules", None)] == ""
    assert exceptions[("contract_monthly_snapshots", None)] == "Data bulanan kontrak"
    assert exceptions[("payments", "posting_date")] == "Tgl posting"
    assert exceptions[("customers", "income_band")] == "Range penghasilan per bulan"
    # every column of collection_actions is expanded from section 3
    assert {c for t, c in exceptions if t == "collection_actions" and c} == {
        c for c, _ in doc_columns()["collection_actions"]
    }
    # 4 tables + 8 collection_actions columns + 5 + 1 + 2 + 4 + 1 + 1 + 1 + 1 + 1 + 1 + 1 columns
    assert len(exceptions) == 4 + 8 + 5 + 1 + 2 + 4 + 1 + 1 + 1 + 1 + 1 + 1 + 1


def test_listed_objects_have_exactly_the_listed_description() -> None:
    for (table, column), expected in doc_catalog_exceptions().items():
        spec = get_table(table)
        actual = (
            spec.catalog_description if column is None else spec.column(column).catalog_description
        )
        assert actual == expected, (table, column)


def test_every_other_object_has_a_non_empty_english_description() -> None:
    listed = doc_catalog_exceptions()
    checked = 0
    for spec in TABLES:
        objects = [(spec.name, None, spec.catalog_description)] + [
            (spec.name, c.name, c.catalog_description) for c in spec.columns
        ]
        for table, column, description in objects:
            if (table, column) in listed:
                continue
            checked += 1
            where = (table, column)
            assert description.strip(), where
            assert description.isascii(), where
            assert not (set(re.findall(r"[a-z]+", description.lower())) & INDONESIAN_WORDS), where
    assert checked > 50


def test_poor_descriptions_do_not_touch_the_personal_columns() -> None:
    customers = get_table("customers")
    for name in customers.personal_columns:
        column = customers.column(name)
        assert column.is_personal
        assert column.catalog_description
        assert ("customers", name) not in doc_catalog_exceptions()
    # the true meaning is kept apart from the (sometimes poor) catalog text
    assert get_table("collection_actions").column("hasil").meaning.startswith("Outcome")
    assert get_table("collection_actions").column("hasil").catalog_description == ""


@pytest.mark.parametrize("spec", TABLES, ids=lambda t: t.name)
def test_every_object_keeps_its_true_meaning(spec: TableSpec) -> None:
    assert spec.meaning.strip()
    assert all(c.meaning.strip() for c in spec.columns)
