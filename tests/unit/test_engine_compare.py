"""WP-11 criterion 5, the pure part: catalog tables against engine tables."""

from __future__ import annotations

import pytest

from makna.engine.base import EngineColumn, EngineTable
from makna.engine.compare import Difference, catalog_schemas, compare, schema_and_table


def table(schema: str, name: str, *columns: str) -> EngineTable:
    return EngineTable(schema, name, tuple(EngineColumn(c, "text") for c in columns))


CATALOG = {
    "lending.core.customers": ["customer_id", "city"],
    "lending.core.branches": ["branch_id"],
}
ENGINE = [table("core", "customers", "customer_id", "city"), table("core", "branches", "branch_id")]


def test_equal_sides_have_no_differences() -> None:
    assert compare(CATALOG, ENGINE) == []


def test_a_column_missing_from_the_engine() -> None:
    engine = [table("core", "customers", "customer_id"), ENGINE[1]]
    assert compare(CATALOG, engine) == [Difference("missing_column", "core", "customers", "city")]


def test_a_column_only_the_engine_has() -> None:
    engine = [table("core", "customers", "customer_id", "city", "nickname"), ENGINE[1]]
    found = compare(CATALOG, engine)
    assert found == [Difference("extra_column", "core", "customers", "nickname")]
    assert found[0].name == "core.customers.nickname"


def test_a_table_missing_from_the_engine_and_a_table_missing_from_the_catalog() -> None:
    engine = [ENGINE[0], table("core", "audit_log", "id")]
    assert compare(CATALOG, engine) == [
        Difference("extra_table", "core", "audit_log"),
        Difference("missing_table", "core", "branches"),
    ]


def test_a_missing_table_is_one_difference_not_one_per_column() -> None:
    assert compare(CATALOG, [ENGINE[1]]) == [Difference("missing_table", "core", "customers")]


def test_names_are_compared_without_regard_to_case() -> None:
    engine = [table("CORE", "Customers", "Customer_ID", "CITY"), ENGINE[1]]
    assert compare(CATALOG, engine) == []


def test_the_catalog_part_of_the_name_is_not_compared() -> None:
    assert compare({"other.core.branches": ["branch_id"]}, [ENGINE[1]]) == []


def test_engine_tables_in_a_schema_the_catalog_does_not_cover_are_out_of_scope() -> None:
    assert compare(CATALOG, [*ENGINE, table("staging", "tmp", "x")]) == []


def test_differences_come_sorted_by_table_and_column() -> None:
    engine = [table("core", "customers", "z", "a"), table("core", "branches")]
    assert [d.name for d in compare(CATALOG, engine)] == [
        "core.branches.branch_id",
        "core.customers.a",
        "core.customers.city",
        "core.customers.customer_id",
        "core.customers.z",
    ]


def test_schemas_to_introspect() -> None:
    assert catalog_schemas({"a.core.t": [], "a.mart.u": [], "a.core.v": []}) == ["core", "mart"]


@pytest.mark.parametrize("bad", ["core.customers", "a.b.c.d", "a..c", ""])
def test_an_engine_fqn_must_have_three_parts(bad: str) -> None:
    with pytest.raises(ValueError, match="catalog.schema.table"):
        schema_and_table(bad)
