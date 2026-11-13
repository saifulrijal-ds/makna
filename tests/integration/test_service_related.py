"""WP-12 criterion 4 (`list_related`), its limits, and what it must not list."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine

from makna.index import indexer
from makna.model.definitions import Metric, parse_definition
from makna.model.objects import DefinitionObject
from makna.search.embedding import HashProvider
from makna.services import ServiceError
from makna.services.errors import ServiceErrorCode
from makna.services.related import MAX_RELATED, RELATIONS

from ..catalog_support import DEFINITIONS
from . import service_support as sup
from .service_support import Users


def related(world: tuple[Engine, Users], role: str, object_id: str, **kw) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    engine, users = world
    found = sup.services(engine).list_related(users.caller(role), object_id, **kw)
    return [(item.id, item.relation) for item in found.related]


def metrics_using(rule: str) -> set[str]:
    """Metrics that use `rule` itself or through their inputs, read straight from the YAML files
    (independent of the index)."""
    entities: dict[str, dict] = {}  # type: ignore[type-arg]
    metrics: dict[str, dict] = {}  # type: ignore[type-arg]
    for path in DEFINITIONS.rglob("*.yaml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if doc["kind"] == "entity":
            entities[doc["name"]] = doc
        elif doc["kind"] == "metric":
            metrics[doc["name"]] = doc
    entity, _, name = rule.partition(".")

    def direct(metric: dict) -> bool:  # type: ignore[type-arg]
        if metric["type"] != "simple":
            return False
        measure_entity, _, measure_name = metric["measure"].partition(".")
        measure = next(m for m in entities[measure_entity]["measures"] if m["name"] == measure_name)
        if measure_entity == entity and measure.get("rule") == name:
            return True
        return any(f.get("rule") == rule for f in metric.get("filters", []))

    def uses(metric: dict, seen: frozenset[str] = frozenset()) -> bool:  # type: ignore[type-arg]
        if direct(metric):
            return True
        inputs = (
            [metric["numerator"], metric["denominator"]]
            if metric["type"] == "ratio"
            else metric.get("inputs", [])
        )
        return any(uses(metrics[i]) for i in inputs if i not in seen)

    return {f"metric:{n}" for n, m in metrics.items() if uses(m)}


def test_a_ratio_metric_lists_its_inputs_its_rule_and_the_term_that_explains_it(
    service_world: tuple[Engine, Users],
) -> None:
    found = related(service_world, "viewer", "metric:npf_ratio")
    assert ("metric:npf_outstanding_principal", "input_of") in found
    assert ("metric:outstanding_principal", "input_of") in found
    assert ("rule:contract_snapshot.non_performing", "uses_rule") in found
    assert ("glossary_term:npf", "explains") in found
    assert ("entity:contract_snapshot", "measured_on") in found
    assert ("metric:par30_ratio", "same_entity") in found
    assert ("metric:npl_ratio", "replaces") in found  # the retired metric this one replaces


def test_a_rule_lists_every_metric_that_uses_it(service_world: tuple[Engine, Users]) -> None:
    rule = "rule:contract_snapshot.non_performing"
    found = related(service_world, "viewer", rule)
    expected = metrics_using("contract_snapshot.non_performing")
    assert {i for i, _ in found} == expected - {"metric:npl_ratio"}  # deprecated: hidden
    assert {relation for _, relation in found} == {"used_by"}
    assert {"metric:npf_outstanding_principal", "metric:npf_ratio"} <= {i for i, _ in found}
    with_deprecated = related(service_world, "viewer", rule, include_deprecated=True)
    assert {i for i, _ in with_deprecated} == expected


def test_other_rules_list_their_metrics_too(service_world: tuple[Engine, Users]) -> None:
    for rule in ("contract_snapshot.in_book", "contract_snapshot.past_due_30"):
        found = {
            i for i, _ in related(service_world, "curator", f"rule:{rule}", include_deprecated=True)
        }
        assert found == metrics_using(rule), rule


def test_the_relation_filter_is_respected(service_world: tuple[Engine, Users]) -> None:
    only_inputs = related(service_world, "viewer", "metric:npf_ratio", relation="input_of")
    assert {r for _, r in only_inputs} == {"input_of"} and len(only_inputs) == 2
    assert related(service_world, "viewer", "metric:npf_ratio", relation="built_on") == []
    built = related(
        service_world, "viewer", "metric:npf_outstanding_principal", relation="built_on"
    )
    assert ("metric:npf_ratio", "built_on") in built


def test_an_entity_lists_its_metrics_and_the_entities_it_joins(
    service_world: tuple[Engine, Users],
) -> None:
    found = related(service_world, "viewer", "entity:contract_snapshot")
    assert ("metric:outstanding_principal", "measures") in found
    assert ("metric:npf_ratio", "measures") in found
    assert ("entity:contract", "joins_to") in found
    assert ("entity:contract_snapshot", "joined_from") in related(
        service_world, "viewer", "entity:contract"
    )


def test_a_glossary_term_lists_what_it_explains(service_world: tuple[Engine, Users]) -> None:
    found = related(service_world, "viewer", "glossary_term:npf")
    assert {
        ("metric:npf_ratio", "explained"),
        ("metric:npf_outstanding_principal", "explained"),
    } <= set(found)


def test_a_deprecated_metric_names_its_replacement(service_world: tuple[Engine, Users]) -> None:
    found = related(service_world, "viewer", "metric:npl_ratio")
    assert ("metric:npf_ratio", "replaced_by") in found


def test_the_results_are_in_a_fixed_order_with_one_line_descriptions(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    found = sup.services(engine).list_related(users.caller("viewer"), "metric:npf_ratio")
    order = [RELATIONS.index(item.relation) for item in found.related]
    assert order == sorted(order)
    assert all(
        "\n" not in item.description and len(item.description) <= 200 for item in found.related
    )
    assert found.to_dict()["related"][0].keys() == {
        "id",
        "label",
        "kind",
        "relation",
        "description",
    }


# --- visibility ---


def test_a_draft_is_never_listed_for_a_viewer_or_a_token_and_is_for_a_curator(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    for caller in (users.caller("viewer"), users.token()):
        found = {i.id for i in svc.list_related(caller, "entity:contract_snapshot").related}
        assert "metric:write_off_amount" not in found
    found = {
        i.id for i in svc.list_related(users.caller("curator"), "entity:contract_snapshot").related
    }
    assert "metric:write_off_amount" in found


def test_a_hidden_object_is_not_found_whatever_it_is_asked_for(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    for object_id, relation in (
        ("metric:write_off_amount", None),
        ("metric:write_off_amount", "input_of"),
        ("metric:no_such_metric", None),
    ):
        with pytest.raises(ServiceError) as raised:
            svc.list_related(users.caller("viewer"), object_id, relation=relation)
        assert raised.value.code == ServiceErrorCode.NOT_FOUND
        assert "metric:write_off_amount" not in raised.value.details["close_ids"]


def test_a_deprecated_neighbour_is_listed_only_when_asked_for(
    service_world: tuple[Engine, Users],
) -> None:
    plain = related(service_world, "viewer", "metric:npf_ratio", relation="same_entity")
    assert "metric:npl_ratio" not in {i for i, _ in plain}
    asked = related(
        service_world, "viewer", "metric:npf_ratio", relation="same_entity", include_deprecated=True
    )
    assert "metric:npl_ratio" in {i for i, _ in asked}


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        ({"id": "npf_ratio"}, "id"),
        ({"id": "metric:npf_ratio", "relation": "friends_with"}, "relation"),
        ({"id": "metric:npf_ratio", "relation": 3}, "relation"),
        ({"id": "metric:npf_ratio", "include_deprecated": "no"}, "include_deprecated"),
    ],
)
def test_bad_input_is_invalid_input_naming_the_field(
    service_world: tuple[Engine, Users],
    arguments: dict[str, Any],
    field: str,
) -> None:
    engine, users = service_world
    with pytest.raises(ServiceError) as raised:
        sup.services(engine).list_related(users.caller("viewer"), **arguments)
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    assert raised.value.details["field"] == field
    if field == "relation":
        assert raised.value.details["allowed"] == list(RELATIONS)


def test_a_catalog_column_has_nothing_related(service_world: tuple[Engine, Users]) -> None:
    assert related(service_world, "viewer", "column:lending.core.customers.city") == []


# --- the limit ---


def metric_on(name: str, measure: str) -> DefinitionObject:
    parsed = parse_definition(
        {
            "apiVersion": "makna/v1",
            "kind": "metric",
            "name": name,
            "label": name.replace("_", " ").title(),
            "description": "A metric made for the list limit test, on one entity.",
            "domain": "lending",
            "owner": {"team": "test"},
            "status": "approved",
            "sensitivity": "internal",
            "synonyms": {"en": ["x"], "id": ["y"]},
            "type": "simple",
            "measure": measure,
            "format": {"kind": "number"},
        }
    )
    assert isinstance(parsed, Metric)
    return DefinitionObject(official=parsed)


def test_at_most_twenty_results_are_returned(fresh_service_world: tuple[Engine, Users]) -> None:
    engine, users = fresh_service_world
    for index_number in range(25):
        indexer.index_object(
            engine,
            HashProvider(),
            metric_on(f"extra_{index_number:02d}", "contract.disbursed_principal"),
        )
    found = sup.services(engine).list_related(users.caller("viewer"), "entity:contract")
    assert len(found.related) == MAX_RELATED == 20
    assert {i.relation for i in found.related} <= {"explains", "measures"}
    assert len([i for i in found.related if i.relation == "measures"]) >= 15
    everything = [
        i
        for i in sup.services(engine)
        .list_related(users.caller("viewer"), "metric:extra_00")
        .related
    ]
    assert len(everything) <= 20


def test_the_definitions_directory_of_the_sample_is_the_one_used() -> None:
    assert Path(DEFINITIONS).is_dir()
