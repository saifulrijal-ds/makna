"""WP-06 acceptance 3 and 4 (pure part): reference issues R01 to R04 from in-memory inputs."""

from __future__ import annotations

import pytest

from makna.catalog.references import CatalogView, Finding, find_issues
from makna.model.definitions import Entity

from ..catalog_support import dimension, make_entity, make_term

TABLE = "lending.core.things"


def view(
    columns: dict[str, str | None] | None = None, terms: frozenset[str] = frozenset()
) -> CatalogView:
    columns = {"thing_id": None, "city": None, "amount": None} if columns is None else columns
    return CatalogView({TABLE: columns}, terms)


def codes(findings: list[Finding]) -> list[tuple[str, str, str]]:
    return [(f.code, f.object_id, f.part) for f in findings]


def entity_with_everything() -> Entity:
    return make_entity(
        dimensions=[
            dimension("city"),
            dimension("amount_band", "CASE WHEN amount > 5 THEN 'big' ELSE 'small' END"),
        ],
        measures=[
            {
                "name": "total",
                "agg": "sum",
                "expr": "amount",
                "filter": "city IS NOT NULL",
                "time_dimension": "city",
                "description": "Sum of the amount of every thing.",
            }
        ],
        rules=[
            {"name": "big", "description": "A thing with a large amount.", "expr": "amount > 100"}
        ],
    )


def test_a_matching_catalog_has_no_issue() -> None:
    assert find_issues([entity_with_everything()], [], view()) == []


def test_r01_source_table_not_in_the_catalog() -> None:
    findings = find_issues([entity_with_everything()], [], CatalogView({}))
    assert codes(findings) == [("R01", "entity:thing", "source.table")]


def test_r02_names_every_part_that_reads_the_missing_column() -> None:
    findings = find_issues([entity_with_everything()], [], view({"thing_id": None, "city": None}))
    assert codes(findings) == [
        ("R02", "entity:thing", "dimension:amount_band"),
        ("R02", "entity:thing", "measure:total"),
        ("R02", "entity:thing", "rule:big"),
    ]
    assert all("amount" in f.message for f in findings)


def test_r02_primary_key_and_a_dimension_without_expr() -> None:
    findings = find_issues([entity_with_everything()], [], view({"amount": None}))
    assert [(f.code, f.part) for f in findings] == [
        ("R02", "primary_key"),
        ("R02", "dimension:city"),
        ("R02", "measure:total"),
    ]


def test_r02_relationship_keys_on_both_sides() -> None:
    other = make_entity(
        "owner", "lending.core.owners", primary_key=("owner_id",), dimensions=[dimension("xx")]
    )
    thing = make_entity(
        relationships=[
            {
                "name": "owner",
                "to": "owner",
                "keys": [{"from": "owner_ref", "to": "owner_id"}],
                "description": "The owner of the thing.",
            }
        ]
    )
    full = CatalogView(
        {
            TABLE: {"thing_id": None, "owner_ref": None},
            "lending.core.owners": {"owner_id": None, "xx": None},
        }
    )
    assert find_issues([thing, other], [], full) == []
    no_from = CatalogView({**full.tables, TABLE: {"thing_id": None}})
    assert codes(find_issues([thing, other], [], no_from)) == [
        ("R02", "entity:thing", "relationship:owner")
    ]
    no_to = CatalogView({**full.tables, "lending.core.owners": {"xx": None}})
    found = find_issues([thing, other], [], no_to)
    assert [(f.code, f.object_id, f.part) for f in found] == [
        ("R02", "entity:thing", "relationship:owner"),
        ("R02", "entity:owner", "primary_key"),
    ]


def test_r03_a_dimension_declared_below_the_catalog_sensitivity() -> None:
    entity = make_entity(dimensions=[dimension("city", sensitivity="internal")])
    findings = find_issues([entity], [], view({"thing_id": None, "city": "personal"}))
    assert codes(findings) == [("R03", "entity:thing", "dimension:city")]
    assert "internal" in findings[0].message and "personal" in findings[0].message


def test_r03_uses_the_entity_level_when_the_dimension_declares_none() -> None:
    entity = make_entity(sensitivity="confidential", dimensions=[dimension("city")])
    assert codes(find_issues([entity], [], view({"thing_id": None, "city": "personal"}))) == [
        ("R03", "entity:thing", "dimension:city")
    ]
    assert find_issues([entity], [], view({"thing_id": None, "city": "confidential"})) == []


def test_r03_is_not_raised_when_the_dimension_is_as_strict_as_the_catalog() -> None:
    entity = make_entity(
        dimensions=[dimension("city", sensitivity="personal", masking={"method": "redact"})]
    )
    assert find_issues([entity], [], view({"thing_id": None, "city": "personal"})) == []


def test_r04_catalog_term_not_in_the_catalog() -> None:
    terms = [make_term("npf", "glossary.npf"), make_term("tenor"), make_term("par", "PAR")]
    findings = find_issues([], terms, CatalogView({}, frozenset({"PAR"})))
    assert codes(findings) == [("R04", "glossary_term:npf", "catalog_term")]
    assert find_issues([], terms[:1], CatalogView({}, frozenset({"glossary.npf"}))) == []


def test_each_rule_opens_exactly_its_own_issue() -> None:
    """One broken thing at a time: only that rule's code appears (acceptance 3, pure part)."""
    entity = entity_with_everything()
    cases = {
        "R01": find_issues([entity], [], CatalogView({})),
        "R02": find_issues([entity], [], view({"thing_id": None, "city": None})),
        "R03": find_issues(
            [make_entity(dimensions=[dimension("city", sensitivity="internal")])],
            [],
            view({"thing_id": None, "city": "personal"}),
        ),
        "R04": find_issues([], [make_term("npf", "glossary.npf")], CatalogView({})),
    }
    for code, findings in cases.items():
        assert findings and {f.code for f in findings} == {code}, code


# --- R06: the declared primary key against the catalog's -----------------------------------


def keyed_view(*key: str, columns: dict[str, str | None] | None = None) -> CatalogView:
    base = view(columns)
    return CatalogView(base.tables, base.terms, {TABLE: frozenset(key)})


def test_r06_a_declared_key_column_the_catalog_does_not_mark() -> None:
    entity = make_entity(primary_key=("city",), dimensions=[dimension("city")])
    found = find_issues([entity], [], keyed_view("thing_id"))
    assert codes(found) == [("R06", "entity:thing", "primary_key")]
    assert "does not mark city" in found[0].message
    assert "also marks thing_id" in found[0].message


def test_r06_a_declared_key_that_leaves_out_part_of_the_catalog_key() -> None:
    entity = make_entity(primary_key=("thing_id",), dimensions=[dimension("city")])
    found = find_issues([entity], [], keyed_view("thing_id", "city"))
    assert codes(found) == [("R06", "entity:thing", "primary_key")]
    assert "also marks city" in found[0].message


def test_r06_a_declared_key_that_is_the_catalog_key_has_no_issue() -> None:
    entity = make_entity(primary_key=("thing_id", "city"), dimensions=[dimension("city")])
    assert find_issues([entity], [], keyed_view("city", "thing_id")) == []


def test_r06_says_nothing_when_the_catalog_knows_no_key() -> None:
    entity = make_entity(primary_key=("city",), dimensions=[dimension("city")])
    assert find_issues([entity], [], view()) == []
    assert find_issues([entity], [], keyed_view()) == []


def test_r06_leaves_a_missing_column_to_r02() -> None:
    entity = make_entity(primary_key=("nope",), dimensions=[dimension("city")])
    assert codes(find_issues([entity], [], keyed_view("thing_id"))) == [
        ("R02", "entity:thing", "primary_key")
    ]


# --- R07: a mask over a column it cannot take -----------------------------------------------


def typed_view(**types: str) -> CatalogView:
    base = view()
    columns = {name: None for name in types} | dict(base.tables[TABLE])
    return CatalogView({TABLE: columns}, base.terms, column_types={TABLE: dict(types)})


def masked(name: str, method: dict[str, object], sensitivity: str = "personal") -> Entity:
    return make_entity(
        dimensions=[dimension(name, sensitivity=sensitivity, masking=method)],
    )


def test_r07_a_bucket_mask_over_a_text_column() -> None:
    entity = masked("amount", {"method": "bucket", "width": 10})
    found = find_issues([entity], [], typed_view(amount="string"))
    assert codes(found) == [("R07", "entity:thing", "dimension:amount")]
    assert "string" in found[0].message and "bucket" in found[0].message


def test_r07_a_truncate_date_mask_over_a_column_that_is_not_a_date() -> None:
    entity = masked("amount", {"method": "truncate_date", "grain": "year"})
    assert codes(find_issues([entity], [], typed_view(amount="integer"))) == [
        ("R07", "entity:thing", "dimension:amount")
    ]


@pytest.mark.parametrize(
    ("method", "data_type"),
    [
        ({"method": "bucket", "width": 10}, "integer"),
        ({"method": "bucket", "width": 10}, "decimal"),
        ({"method": "bucket", "width": 10}, "float"),
        ({"method": "truncate_date", "grain": "year"}, "date"),
        ({"method": "truncate_date", "grain": "year"}, "timestamp"),
        ({"method": "bucket", "width": 10}, "other"),
        ({"method": "redact"}, "string"),
    ],
)
def test_r07_says_nothing_when_the_mask_can_take_the_column(
    method: dict[str, object], data_type: str
) -> None:
    assert find_issues([masked("amount", method)], [], typed_view(amount=data_type)) == []


def test_r07_leaves_a_dimension_that_is_not_personal_alone() -> None:
    entity = masked("amount", {"method": "bucket", "width": 10}, sensitivity="internal")
    assert find_issues([entity], [], typed_view(amount="string")) == []


def test_r07_follows_the_catalog_tag_that_makes_a_dimension_personal() -> None:
    entity = masked("amount", {"method": "bucket", "width": 10}, sensitivity="internal")
    base = typed_view(amount="string")
    tagged = CatalogView(
        {TABLE: {**base.tables[TABLE], "amount": "personal"}},
        base.terms,
        column_types=base.column_types,
    )
    # R03 says the dimension is declared below the catalog level; R07 is the mask problem
    assert ("R07", "entity:thing", "dimension:amount") in codes(find_issues([entity], [], tagged))
