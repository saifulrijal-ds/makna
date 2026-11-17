"""WP-15C (C-10, D-32 ruling 4): a deprecated entity that a request reaches stays usable.

The request compiles, and the response carries a warning with the entity, the reason and the
replacement from the entity's lifecycle fields. Metrics are different: a deprecated metric is
refused (`DEPRECATED`), and that has not changed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from makna.compiler.compile import compile_request
from makna.compiler.plan import plan_request
from makna.compiler.snapshot import Model
from makna.model.request import ErrorCode, QueryError

from .compiler_support import entity_doc, lending, metric_doc, request, shop
from .definition_fixtures import Files


def deprecate(name: str, *, replaced_by: str | None = "entity:area") -> Callable[[Files], None]:
    def mutate(files: Files) -> None:
        doc = entity_doc(files, name)
        doc["status"] = "deprecated"
        doc["deprecation"] = {"reason": "Regions were merged into areas in 2026."}
        if replaced_by is not None:
            doc["deprecation"]["replaced_by"] = replaced_by

    return mutate


def warnings_of(model: Model, **fields: Any) -> tuple[str, ...]:
    return compile_request(model, request("revenue", **fields)).warnings


def test_a_request_that_joins_a_deprecated_entity_compiles_with_a_warning() -> None:
    model = shop(deprecate("region"))
    response = compile_request(model, request("revenue", dimensions=["region.region_name"]))
    assert response.sql
    text = " ".join(response.warnings)
    assert "Region" in text and "(region)" in text
    assert "deprecated" in text
    assert "Regions were merged into areas in 2026" in text  # the reason
    assert "entity:area" in text  # the replacement
    assert len(response.warnings) == 1


def test_a_deprecated_entity_the_request_does_not_reach_adds_no_warning() -> None:
    model = shop(deprecate("region"))
    assert warnings_of(model) == ()  # no dimension: revenue never joins region
    assert warnings_of(model, dimensions=["order.status"]) == ()


def test_a_deprecated_entity_on_the_way_to_the_dimension_warns_too() -> None:
    """revenue reaches region through customer, so deprecating customer warns for region."""
    model = shop(deprecate("customer", replaced_by=None))
    warnings = warnings_of(model, dimensions=["region.region_name"])
    assert len(warnings) == 1
    assert "(customer)" in warnings[0] and "No replacement is named" in warnings[0]


def test_a_deprecated_measure_entity_warns() -> None:
    model = shop(deprecate("order"))
    warnings = warnings_of(model)
    assert len(warnings) == 1 and "(order)" in warnings[0]


def test_each_deprecated_entity_warns_once_in_the_order_of_their_names() -> None:
    model = shop(deprecate("region"), deprecate("customer"))
    warnings = warnings_of(model, dimensions=["region.region_name", "customer.segment"])
    assert [w.split("(")[1].split(")")[0] for w in warnings] == ["customer", "region"]


def test_the_plan_names_the_deprecated_entities_with_reason_and_replacement() -> None:
    model = shop(deprecate("region"))
    plan = plan_request(model, request("revenue", dimensions=["region.region_name"]))
    (found,) = plan.deprecated_entities
    assert (found.entity, found.replaced_by) == ("region", "entity:area")
    assert found.reason.startswith("Regions were merged")


def test_an_approved_entity_adds_no_warning() -> None:
    response = compile_request(
        lending(), request("disbursement_amount", dimensions=["branch.region_name"])
    )
    assert response.warnings == ()


def test_a_deprecated_metric_is_still_refused() -> None:
    def retire(files: Files) -> None:
        doc = metric_doc(files, "revenue")
        doc["status"] = "deprecated"
        doc["deprecation"] = {"reason": "Replaced."}

    try:
        plan_request(shop(retire), request("revenue"))
    except QueryError as error:
        assert error.code is ErrorCode.DEPRECATED
    else:
        raise AssertionError("a deprecated metric compiled")
