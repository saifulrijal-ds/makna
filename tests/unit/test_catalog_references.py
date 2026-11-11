"""WP-06 acceptance 3 and 4 (pure part): reference issues R01 to R04 from in-memory inputs."""

from __future__ import annotations

from makna.catalog.references import CatalogView, Finding, columns_read, find_issues
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


def test_columns_are_extracted_from_expressions_with_sqlglot() -> None:
    assert columns_read("CAST(Registered_At AS DATE)") == {"registered_at"}
    assert columns_read("sts_putusan IN ('approved', 'rejected')") == {"sts_putusan"}
    assert columns_read("NOT is_reversed") == {"is_reversed"}
    assert columns_read(None) == set()
    assert columns_read("a b c (") == set()
