"""WP-10 criteria 2, 3, 4: personal data is refused, and the effective sensitivity decides.

Section 7.6: a personal dimension cannot be selected without a masking rule and can never be
filtered on; the effective level is the stricter of the YAML level and the level derived from
catalog tags (passed to the compiler as data).
"""

from __future__ import annotations

import dataclasses

import pytest

from makna.compiler.compile import compile_request
from makna.compiler.plan import available_dimensions, plan_request
from makna.compiler.snapshot import Model
from makna.model.definitions import Dimension, Entity
from makna.model.request import ErrorCode, QueryError

from .compiler_support import lending, request, shop

METRIC = "outstanding_principal"


def with_derived(model: Model, **levels: str) -> Model:
    """`model` with catalog-derived levels; keys are written `customer__email` for brevity."""
    derived = {key.replace("__", ".", 1): level for key, level in levels.items()}
    return dataclasses.replace(model, derived_sensitivity=derived)


def refusal(model: Model, **fields: object) -> QueryError:
    with pytest.raises(QueryError) as caught:
        plan_request(model, request(METRIC, **fields))
    return caught.value


@pytest.mark.parametrize(
    "name", ["full_name", "national_id", "email", "phone_number"], ids=lambda n: n
)
def test_selecting_a_personal_dimension_without_masking_is_refused(name: str) -> None:
    error = refusal(lending(), dimensions=[f"customer.{name}"])
    assert error.code == ErrorCode.PERSONAL_DATA_REFUSED
    assert error.details["dimension"] == f"customer.{name}"
    assert error.details["masked_form_available"] is False
    assert "personal" in error.message


@pytest.mark.parametrize(
    "name", ["birth_year", "full_name", "national_id", "email", "phone_number"], ids=lambda n: n
)
def test_a_filter_on_any_personal_dimension_is_refused_masked_or_not(name: str) -> None:
    flt = {"dimension": f"customer.{name}", "op": "=", "value": "x"}
    error = refusal(lending(), filters=[flt])
    assert error.code == ErrorCode.PERSONAL_DATA_REFUSED
    assert error.details["dimension"] == f"customer.{name}"
    assert error.details["use"] == "filter"
    assert error.details["masked_form_available"] is (name == "birth_year")


def test_a_filter_is_refused_before_its_value_is_judged() -> None:
    # An operator that does not exist would be INVALID_FILTER on any other dimension.
    flt = {"dimension": "customer.national_id", "op": "like", "value": "99%"}
    assert refusal(lending(), filters=[flt]).code == ErrorCode.PERSONAL_DATA_REFUSED


def test_a_masked_personal_dimension_can_be_selected() -> None:
    plan = plan_request(lending(), request(METRIC, dimensions=["customer.birth_year"]))
    assert [d.sensitivity for d in plan.dimensions] == ["personal"]


def test_a_dimension_declared_internal_with_a_personal_derived_level_is_personal() -> None:
    model = with_derived(lending(), customer__customer_segment="personal")
    error = refusal(model, dimensions=["customer.customer_segment"])
    assert error.code == ErrorCode.PERSONAL_DATA_REFUSED
    flt = {"dimension": "customer.customer_segment", "op": "=", "value": "mass"}
    assert refusal(model, filters=[flt]).code == ErrorCode.PERSONAL_DATA_REFUSED


def test_the_same_dimension_is_fine_without_the_derived_level() -> None:
    plan = plan_request(lending(), request(METRIC, dimensions=["customer.customer_segment"]))
    assert plan.dimensions[0].sensitivity == "internal"


def test_available_dimensions_list_the_derived_level_and_the_missing_mask() -> None:
    model = with_derived(lending(), customer__customer_segment="personal")
    listed = available_dimensions(model, model.metrics[METRIC])
    option = next(o for o in listed.options if o.ref == "customer.customer_segment")
    assert option.sensitivity == "personal"
    assert option.available is False
    assert "masking" in (option.reason or "")
    assert "customer.customer_segment" not in listed.refs()


def test_a_derived_level_never_loosens_the_declared_one() -> None:
    model = with_derived(lending(), customer__birth_year="public", customer__email="internal")
    assert model.effective_sensitivity(*_found(model, "customer.birth_year")) == "personal"
    assert refusal(model, dimensions=["customer.email"]).code == ErrorCode.PERSONAL_DATA_REFUSED


def test_a_dimension_without_its_own_level_takes_the_entity_level_then_the_derived_one() -> None:
    model = shop()
    entity, dimension = _found(model, "region.region_name")
    assert dimension.sensitivity is None
    assert model.effective_sensitivity(entity, dimension) == entity.sensitivity
    tightened = with_derived(model, region__region_name="confidential")
    assert tightened.effective_sensitivity(entity, dimension) == "confidential"


def test_a_derived_level_with_a_mask_in_the_definition_is_masked_not_refused() -> None:
    model = with_derived(shop(), order__item_count="personal")  # bucket mask, not personal in YAML
    plan = plan_request(model, request("revenue", dimensions=["order.item_count"]))
    assert plan.dimensions[0].sensitivity == "personal"


def test_an_unknown_derived_level_is_rejected_when_the_model_is_built() -> None:
    with pytest.raises(ValueError, match="sensitivity"):
        Model.build([], [], derived_sensitivity={"customer.email": "secret"})


# ---- order of errors (section 7.7) ----------------------------------------------------


def test_unknown_comes_before_personal() -> None:
    error = refusal(lending(), dimensions=["customer.no_such_thing"])
    assert error.code == ErrorCode.UNKNOWN_DIMENSION


def test_personal_refusal_comes_before_not_available() -> None:
    # `new_customer_count` is measured on customer, which reaches no contract. A personal
    # contract dimension is refused as personal data, not reported as "not available".
    model = with_derived(lending(), contract__contract_status="personal")
    error = _plan_error(model, "new_customer_count", dimensions=["contract.contract_status"])
    assert error.code == ErrorCode.PERSONAL_DATA_REFUSED


def test_an_unreachable_ordinary_dimension_is_still_not_available() -> None:
    error = _plan_error(lending(), "new_customer_count", dimensions=["contract.contract_status"])
    assert error.code == ErrorCode.DIMENSION_NOT_AVAILABLE


def test_dimensions_are_judged_in_request_order_then_filters() -> None:
    flt = {"dimension": "customer.email", "op": "=", "value": "x"}
    both = refusal(lending(), dimensions=["customer.full_name"], filters=[flt])
    assert both.details["dimension"] == "customer.full_name"
    first = refusal(lending(), dimensions=["customer.nope", "customer.full_name"])
    assert first.code == ErrorCode.UNKNOWN_DIMENSION
    second = refusal(lending(), dimensions=["customer.full_name", "customer.nope"])
    assert second.code == ErrorCode.PERSONAL_DATA_REFUSED


def test_a_refused_request_compiles_nothing() -> None:
    with pytest.raises(QueryError):
        compile_request(lending(), request(METRIC, dimensions=["customer.email"]))


def _found(model: Model, ref: str) -> tuple[Entity, Dimension]:
    found = model.dimension(ref)
    assert found is not None
    return found


def _plan_error(model: Model, metric: str, **fields: object) -> QueryError:
    with pytest.raises(QueryError) as caught:
        plan_request(model, request(metric, **fields))
    return caught.value
