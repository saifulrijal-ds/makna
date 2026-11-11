"""WP-08 criteria 2 to 6: available dimensions and request planning (sections 7.1 to 7.4, 7.7)."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from makna.compiler.plan import allowed_grains, available_dimensions, plan_request
from makna.compiler.snapshot import Model, ReferenceIssue
from makna.model.request import ErrorCode, QueryError, QueryRequest

from .compiler_support import add_relationship, lending, metric_doc, shop
from .definition_fixtures import Files


def request(metric: str, **fields: Any) -> QueryRequest:
    return QueryRequest.model_validate({"metric": metric, **fields})


def failure(model: Model, req: QueryRequest, **options: Any) -> QueryError:
    with pytest.raises(QueryError) as info:
        plan_request(model, req, **options)
    return info.value


def refs(model: Model, metric: str) -> set[str]:
    return set(available_dimensions(model, model.metrics[metric]).refs())


# --------------------------------------------------------------------------------------
# Criterion 2: available dimensions


def test_npf_ratio_offers_region_and_category_but_not_payment_channel() -> None:
    offered = refs(lending(), "npf_ratio")
    assert {"branch.region_name", "product.product_category"} <= offered
    assert "payment.payment_channel" not in offered
    assert not {r for r in offered if r.startswith("payment.")}


def test_a_simple_metric_offers_the_dimensions_of_every_entity_it_reaches() -> None:
    offered = refs(lending(), "outstanding_principal")
    assert "contract_snapshot.snapshot_date" in offered  # its own entity
    assert "customer.customer_segment" in offered  # contract > customer
    assert "application.channel" in offered  # contract > application
    assert "installment.due_date" not in offered  # nothing leads to installment from a snapshot


def test_a_personal_dimension_without_masking_is_listed_as_unavailable_with_a_reason() -> None:
    result = available_dimensions(lending(), lending().metrics["outstanding_principal"])
    by_ref = {o.ref: o for o in result.options}
    assert by_ref["customer.full_name"].available is False
    assert "masking" in (by_ref["customer.full_name"].reason or "")
    assert by_ref["customer.birth_year"].available is True
    assert by_ref["customer.birth_year"].masking == "truncate_date"
    assert "customer.full_name" not in result.refs()


def test_an_option_names_the_path_each_input_takes() -> None:
    result = available_dimensions(lending(), lending().metrics["npf_ratio"])
    region = next(o for o in result.options if o.ref == "branch.region_name")
    assert {(p.metric, p.steps) for p in region.paths} == {
        (name, ("contract_snapshot.contract", "contract.servicing_branch"))
        for name in ("npf_outstanding_principal", "outstanding_principal")
    }


def test_entities_that_need_an_unmade_join_choice_are_left_out_with_a_warning() -> None:
    # A metric that does not pin a branch: contract reaches branch two ways.
    model = shop(lambda files: add_relationship(files, "order", "billing_region", "region"),
                 lambda files: add_relationship(files, "order", "shipping_region", "region"),
                 lambda files: metric_doc(files, "revenue").pop("join_paths"))  # fmt: skip
    result = available_dimensions(model, model.metrics["revenue"])
    assert not {r for r in result.refs() if r.startswith("region.")}
    assert any("billing_region" in w and "shipping_region" in w for w in result.warnings)
    assert any("join_paths" in w for w in result.warnings)


def _two_region_links(files: Files) -> None:
    add_relationship(files, "order", "billing_region", "region")
    add_relationship(files, "order", "shipping_region", "region")
    # order_count enters region through the billing link, revenue through the shipping link.
    metric_doc(files, "order_count")["join_paths"] = {"region": ["order.billing_region"]}
    metric_doc(files, "revenue")["join_paths"] = {"region": ["order.shipping_region"]}


def test_a_ratio_whose_inputs_pin_different_links_offers_no_dimension_of_that_entity() -> None:
    model = shop(_two_region_links)
    assert "region.region_name" in refs(model, "revenue")
    assert "region.region_name" in refs(model, "order_count")
    ratio = available_dimensions(model, model.metrics["revenue_per_order"])
    assert not {r for r in ratio.refs() if r.startswith("region.")}
    assert any(e.entity == "region" and e.kind == "inconsistent" for e in ratio.excluded)
    assert ratio.warnings and "different relationships" in ratio.warnings[0]


def test_a_derived_metric_follows_the_same_rule() -> None:
    model = shop(_two_region_links)
    assert not {r for r in refs(model, "average_order_value") if r.startswith("region.")}


def test_a_ratio_whose_inputs_pin_the_same_link_offers_the_entity() -> None:
    assert "region.region_name" in refs(shop(), "revenue_per_order")


def test_a_ratio_offers_an_entity_only_when_every_input_reaches_it() -> None:
    # collected_amount reaches the contract; application metrics do not.
    model = lending()
    assert "branch.region_name" in refs(model, "collection_rate")
    assert "installment.due_date" not in refs(model, "collection_rate")  # inconsistent entry


def test_available_dimensions_is_a_pure_function() -> None:
    model = lending()
    metric = model.metrics["collection_rate"]
    assert available_dimensions(model, metric) == available_dimensions(model, metric)


# --------------------------------------------------------------------------------------
# Criterion 4: the status gate


def test_planning_a_draft_metric_is_refused_as_not_approved() -> None:
    error = failure(lending(), request("write_off_amount"))
    assert error.code == ErrorCode.NOT_APPROVED
    assert error.details == {"metric": "write_off_amount", "status": "draft"}


def test_planning_a_deprecated_metric_is_refused_with_its_replacement() -> None:
    error = failure(lending(), request("npl_ratio"))
    assert error.code == ErrorCode.DEPRECATED
    assert error.details["replaced_by"] == "metric:npf_ratio"
    assert "Older name" in error.details["reason"]
    assert "metric:npf_ratio" in error.message


def test_an_input_that_is_not_approved_is_refused_by_name() -> None:
    def draft_input(files: Files) -> None:
        metric_doc(files, "order_count")["status"] = "in_review"

    error = failure(shop(draft_input), request("revenue_per_order"))
    assert error.code == ErrorCode.NOT_APPROVED
    assert error.details == {"metric": "order_count", "status": "in_review"}


def test_the_status_gate_can_be_skipped_for_the_example_request_check() -> None:
    plan = plan_request(lending(), request("write_off_amount"), enforce_status=False)
    assert plan.metric == "write_off_amount"
    plan = plan_request(lending(), request("npl_ratio"), enforce_status=False)
    assert [n.name for n in plan.nodes][-1] == "npl_ratio"


# --------------------------------------------------------------------------------------
# Criterion 3: each error code that planning can raise


def test_unknown_metric_offers_up_to_three_close_names() -> None:
    error = failure(lending(), request("npf_ration"))
    assert error.code == ErrorCode.UNKNOWN_METRIC
    assert error.details["close_names"][0] == "npf_ratio"
    assert len(error.details["close_names"]) <= 3


def test_unknown_metric_with_nothing_close_has_an_empty_list() -> None:
    assert failure(lending(), request("zzz")).details["close_names"] == []


def test_broken_reference_from_an_open_reference_issue() -> None:
    issue = ReferenceIssue("R02", "column days_past_due is not in the table")
    base = lending()
    model = Model.build(
        base.entities.values(), base.metrics.values(), {"contract_snapshot": [issue]}
    )
    error = failure(model, request("npf_ratio"))
    assert error.code == ErrorCode.BROKEN_REFERENCE
    assert error.details["issues"] == [
        {"code": "R02", "object": "entity:contract_snapshot", "message": issue.message}
    ]
    plan_request(model, request("approval_rate"))  # metrics on other entities are not affected


def test_an_issue_on_an_entity_reached_only_by_a_dimension_also_refuses() -> None:
    issue = ReferenceIssue("R01", "source.table is not in the synced catalog")
    base = lending()
    model = Model.build(base.entities.values(), base.metrics.values(), {"branch": [issue]})
    plan_request(model, request("npf_ratio", dimensions=["product.product_category"]))
    error = failure(model, request("npf_ratio", dimensions=["branch.region_name"]))
    assert error.code == ErrorCode.BROKEN_REFERENCE
    assert error.details["issues"][0]["object"] == "entity:branch"


def test_broken_reference_when_a_definition_does_not_resolve() -> None:
    def break_measure(files: Files) -> None:
        metric_doc(files, "revenue")["measure"] = "order.no_such_measure"

    error = failure(shop(break_measure), request("revenue_per_order"))
    assert error.code == ErrorCode.BROKEN_REFERENCE
    assert "no_such_measure" in error.details["issues"][0]["message"]


def test_broken_reference_when_an_input_is_not_a_metric() -> None:
    def break_input(files: Files) -> None:
        metric_doc(files, "revenue_per_order")["numerator"] = "nothing"

    error = failure(shop(break_input), request("revenue_per_order"))
    assert error.code == ErrorCode.BROKEN_REFERENCE


def test_unknown_dimension_lists_what_is_available() -> None:
    for ref in ("branch.no_such_column", "no_entity.region_name", "region_name"):
        error = failure(lending(), request("npf_ratio", dimensions=[ref]))
        assert error.code == ErrorCode.UNKNOWN_DIMENSION
        assert error.details["dimension"] == ref
        assert "branch.region_name" in error.details["available_dimensions"]


def test_unknown_dimension_in_a_filter() -> None:
    flt = {"dimension": "branch.nope", "op": "=", "value": "x"}
    error = failure(lending(), request("npf_ratio", filters=[flt]))
    assert error.code == ErrorCode.UNKNOWN_DIMENSION


def test_dimension_not_available_gives_a_reason_and_the_alternatives() -> None:
    error = failure(lending(), request("npf_ratio", dimensions=["payment.payment_channel"]))
    assert error.code == ErrorCode.DIMENSION_NOT_AVAILABLE
    assert "payment" in error.details["reason"]
    assert "branch.region_name" in error.details["available_dimensions"]
    assert "payment.payment_channel" not in error.details["available_dimensions"]


def test_dimension_not_available_in_a_filter() -> None:
    flt = {"dimension": "payment.payment_channel", "op": "=", "value": "cash"}
    error = failure(lending(), request("npf_ratio", filters=[flt]))
    assert error.code == ErrorCode.DIMENSION_NOT_AVAILABLE


def test_an_ambiguous_entity_is_reported_as_not_available_with_the_candidates() -> None:
    model = shop(
        lambda files: add_relationship(files, "order", "billing_region", "region"),
        lambda files: add_relationship(files, "order", "shipping_region", "region"),
        lambda files: metric_doc(files, "revenue").pop("join_paths"),
    )
    error = failure(model, request("revenue", dimensions=["region.region_name"]))
    assert error.code == ErrorCode.DIMENSION_NOT_AVAILABLE
    assert error.details["candidate_paths"] == [["order.billing_region"], ["order.shipping_region"]]


def test_a_ratio_with_inconsistent_inputs_cannot_group_by_that_entity() -> None:
    error = failure(
        shop(_two_region_links), request("revenue_per_order", dimensions=["region.region_name"])
    )
    assert error.code == ErrorCode.DIMENSION_NOT_AVAILABLE
    assert "different relationships" in error.details["reason"]


def test_a_definition_filter_on_an_ambiguous_entity_raises_the_path_error() -> None:
    def filter_on_region(files: Files) -> None:
        add_relationship(files, "order", "billing_region", "region")
        add_relationship(files, "order", "shipping_region", "region")
        metric = metric_doc(files, "revenue")
        metric.pop("join_paths")
        metric["filters"].append({"dimension": "region.region_name", "op": "=", "value": "north"})

    error = failure(shop(filter_on_region), request("revenue"))
    assert error.code == ErrorCode.AMBIGUOUS_JOIN_PATH
    assert error.details["metric"] == "revenue"


def test_a_definition_filter_on_an_unreachable_entity_raises_no_join_path() -> None:
    def filter_on_order(files: Files) -> None:
        metric = metric_doc(files, "revenue")
        metric["measure"] = "customer.signups"
        metric["filters"] = [{"dimension": "order.status", "op": "=", "value": "open"}]
        metric.pop("join_paths")

    error = failure(shop(filter_on_order), request("revenue"))
    assert error.code == ErrorCode.NO_JOIN_PATH


def test_invalid_grain_lists_the_allowed_grains() -> None:
    error = failure(lending(), request("npf_ratio", grain="day"))
    assert error.code == ErrorCode.INVALID_GRAIN
    assert error.details["allowed_grains"] == ["month", "quarter", "year"]


def test_a_grain_that_does_not_exist_is_invalid_too() -> None:
    error = failure(lending(), request("collection_rate", grain="fortnight"))
    assert error.code == ErrorCode.INVALID_GRAIN
    assert error.details["allowed_grains"] == ["day", "week", "month", "quarter", "year"]


def test_a_ratio_offers_only_the_grains_its_inputs_offer() -> None:
    def narrow(files: Files) -> None:
        metric_doc(files, "order_count")["time_grains"] = ["month"]

    model = shop(narrow)
    assert allowed_grains(model, model.metrics["order_count"]) == ("month",)
    assert allowed_grains(model, model.metrics["revenue_per_order"]) == ("month",)
    assert (
        failure(model, request("revenue_per_order", grain="year")).code == ErrorCode.INVALID_GRAIN
    )


@pytest.mark.parametrize(
    ("flt", "reason"),
    [
        ({"dimension": "order.status", "op": "like", "value": "o"}, "operator"),
        ({"dimension": "order.status", "op": "=", "value": "lost"}, "not one of"),
        ({"dimension": "order.status", "op": "in", "value": "open"}, "list"),
        ({"dimension": "order.status", "op": "in", "value": []}, "list"),
        ({"dimension": "order.status", "op": "=", "value": ["open"]}, "one value"),
        ({"dimension": "order.status", "op": "=", "value": 3}, "not one of"),
        ({"dimension": "order.status", "op": "is_null", "value": "open"}, "no value"),
        ({"dimension": "order.status", "op": "="}, "one value"),
        ({"dimension": "order.is_gift", "op": "=", "value": "yes"}, "boolean"),
        ({"dimension": "order.is_gift", "op": ">", "value": True}, "boolean"),
        ({"dimension": "order.item_count", "op": ">", "value": "many"}, "numeric"),
        ({"dimension": "order.item_count", "op": ">", "value": True}, "numeric"),
        ({"dimension": "order.order_date", "op": ">=", "value": "last week"}, "time"),
        ({"dimension": "order.order_date", "op": ">=", "value": 20260101}, "time"),
    ],
)
def test_invalid_filter_says_what_was_expected(flt: dict[str, Any], reason: str) -> None:
    error = failure(shop(), request("revenue", filters=[flt]))
    assert error.code == ErrorCode.INVALID_FILTER
    assert reason in error.message
    assert error.details["dimension"] == flt["dimension"]
    assert error.details["expected_type"] == shop().dimension(flt["dimension"])[1].type  # type: ignore[index]


def test_invalid_filter_lists_the_allowed_values() -> None:
    flt = {"dimension": "order.status", "op": "=", "value": "lost"}
    error = failure(shop(), request("revenue", filters=[flt]))
    assert error.details["allowed_values"] == ["open", "shipped", "cancelled"]
    assert error.details["expected_type"] == "categorical"


@pytest.mark.parametrize(
    "flt",
    [
        {"dimension": "order.status", "op": "in", "value": ["open", "shipped"]},
        {"dimension": "order.status", "op": "not_in", "value": ["open"]},
        {"dimension": "order.is_gift", "op": "=", "value": True},
        {"dimension": "order.item_count", "op": ">=", "value": 3.5},
        {"dimension": "order.order_date", "op": "<", "value": "2026-07-01"},
        {"dimension": "order.status", "op": "is_not_null"},
    ],
)
def test_a_fitting_filter_plans(flt: dict[str, Any]) -> None:
    plan = plan_request(shop(), request("revenue", filters=[flt]))
    assert plan.filters[0].op == flt["op"]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("last month", None),
        ("2026-13-01", None),
        ("20260101", None),
        ("2026-1-1", None),
        (None, "07/01/2026"),
        ("2026-07-01", "2026-07-01"),
        ("2026-07-02", "2026-07-01"),
    ],
)
def test_invalid_time_range(start: str | None, end: str | None) -> None:
    error = failure(lending(), request("npf_ratio", time_start=start, time_end=end))
    assert error.code == ErrorCode.INVALID_TIME_RANGE


def test_a_time_range_with_one_end_or_none_plans() -> None:
    plan = plan_request(lending(), request("npf_ratio", time_start="2026-01-01"))
    assert plan.time_start == datetime.date(2026, 1, 1) and plan.time_end is None
    plan = plan_request(lending(), request("npf_ratio"))
    assert plan.time_start is None and plan.time_end is None


@pytest.mark.parametrize("limit", [10001, 0, -5])
def test_limit_outside_one_to_ten_thousand(limit: int) -> None:
    error = failure(lending(), request("npf_ratio", limit=limit))
    assert error.code == ErrorCode.LIMIT_EXCEEDED
    assert error.details["max_limit"] == 10000


def test_the_limit_defaults_to_1000_and_10000_is_allowed() -> None:
    assert plan_request(lending(), request("npf_ratio")).limit == 1000
    assert plan_request(lending(), request("npf_ratio", limit=10000)).limit == 10000


def test_the_first_problem_wins_in_a_fixed_order() -> None:
    # unknown metric before anything else; status before request checks
    assert failure(lending(), request("zzz", limit=0)).code == ErrorCode.UNKNOWN_METRIC
    assert failure(lending(), request("write_off_amount", limit=0)).code == ErrorCode.NOT_APPROVED


# --------------------------------------------------------------------------------------
# What the plan holds


def test_a_plan_holds_the_inputs_dimensions_columns_and_paths() -> None:
    plan = plan_request(
        lending(),
        request(
            "npf_ratio",
            dimensions=["branch.region_name", "product.product_category"],
            filters=[{"dimension": "customer.customer_segment", "op": "=", "value": "mass"}],
            grain="quarter",
            time_start="2025-10-01",
            time_end="2026-07-01",
        ),
    )
    assert [n.name for n in plan.nodes] == [
        "npf_outstanding_principal",
        "outstanding_principal",
        "npf_ratio",
    ]
    assert plan.nodes[-1].inputs == ("npf_outstanding_principal", "outstanding_principal")
    assert [i.metric for i in plan.inputs] == ["npf_outstanding_principal", "outstanding_principal"]
    npf = plan.inputs[0]
    assert npf.measure_entity == "contract_snapshot"
    assert npf.measure.name == "npf_outstanding_principal"
    assert (
        npf.measure_rule is not None and npf.measure_rule.ref == "contract_snapshot.non_performing"
    )
    assert npf.measure_rule.expr == "days_past_due > 90"
    assert npf.time_dimension.name == "snapshot_date"
    assert {p.entity for p in npf.paths} == {"branch", "product", "customer"}
    assert [s.name for s in npf.join_steps] == [
        "contract_snapshot.contract",
        "contract.servicing_branch",
        "contract.product",
        "contract.customer",
    ]
    assert [(c.name, c.role) for c in plan.columns] == [
        ("metric_time", "time"),
        ("branch__region_name", "dimension"),
        ("product__product_category", "dimension"),
        ("npf_outstanding_principal", "input"),
        ("outstanding_principal", "input"),
        ("npf_ratio", "metric"),
    ]
    assert plan.columns[-1].unit == "percent"
    assert plan.columns[3].unit == "IDR"
    assert plan.grain == "quarter"
    assert plan.time_end == datetime.date(2026, 7, 1)


def test_a_simple_metric_has_no_input_columns_and_a_repeated_dimension_counts_once() -> None:
    plan = plan_request(
        lending(),
        request("disbursement_amount", dimensions=["branch.region_name", "branch.region_name"]),
    )
    assert [c.name for c in plan.columns] == ["branch__region_name", "disbursement_amount"]
    assert len(plan.dimensions) == 1


def test_planning_a_dimension_of_the_measure_entity_adds_no_join() -> None:
    plan = plan_request(
        lending(), request("outstanding_principal", dimensions=["contract_snapshot.dpd_bucket"])
    )
    assert plan.inputs[0].paths == ()
    assert plan.inputs[0].join_steps == ()


def test_each_input_of_a_ratio_gets_its_own_path_to_the_same_entity() -> None:
    plan = plan_request(
        lending(),
        request("collection_rate", dimensions=["branch.region_name", "customer.province"]),
    )
    paths = {i.metric: {p.entity: [s.name for s in p.steps] for p in i.paths} for i in plan.inputs}
    assert paths["collected_amount"]["branch"] == ["payment.contract", "contract.servicing_branch"]
    assert paths["due_amount"]["branch"] == ["installment.contract", "contract.servicing_branch"]
    assert paths["collected_amount"]["customer"] == ["payment.contract", "contract.customer"]


# --------------------------------------------------------------------------------------
# Criterion 6: rules (D-17)


def test_a_measure_with_a_rule_plans_and_adds_no_join() -> None:
    plan = plan_request(lending(), request("npf_outstanding_principal"))
    rule = plan.inputs[0].measure_rule
    assert rule is not None
    assert (rule.ref, rule.entity, rule.name) == (
        "contract_snapshot.non_performing",
        "contract_snapshot",
        "non_performing",
    )
    assert plan.inputs[0].join_steps == ()
    assert plan.inputs[0].definition_filters == ()


def test_a_metric_filter_rule_on_a_joined_entity_plans_and_adds_the_join_to_contract() -> None:
    model = lending()
    plan = plan_request(model, request("restructured_outstanding_principal"))
    only = plan.inputs[0]
    assert only.measure_rule is None
    (rule,) = only.definition_filters
    assert getattr(rule, "ref") == "contract.restructured"  # noqa: B009
    assert [(p.entity, [s.name for s in p.steps]) for p in only.paths] == [
        ("contract", ["contract_snapshot.contract"])
    ]
    # the same measure without the rule joins nothing
    assert plan_request(model, request("outstanding_principal")).inputs[0].join_steps == ()


def test_a_rule_that_does_not_exist_is_a_broken_reference() -> None:
    def break_rule(files: Files) -> None:
        metric_doc(files, "order_count")["filters"] = [{"rule": "order.no_such_rule"}]

    error = failure(shop(break_rule), request("order_count"))
    assert error.code == ErrorCode.BROKEN_REFERENCE
    assert "order.no_such_rule" in error.details["issues"][0]["message"]


def test_a_structured_metric_filter_is_part_of_the_plan() -> None:
    plan = plan_request(shop(), request("revenue"))
    (flt,) = plan.inputs[0].definition_filters
    assert (getattr(flt, "ref"), getattr(flt, "op"), getattr(flt, "value")) == (  # noqa: B009
        "order.status",
        "!=",
        "cancelled",
    )


# --------------------------------------------------------------------------------------
# Criterion 5: planning is a pure function


def test_the_same_inputs_give_equal_plans() -> None:
    req = request("collection_rate", dimensions=["branch.region_name"], grain="month")
    first = plan_request(lending(), req)
    second = plan_request(lending(), req)
    assert first == second
    assert first is not second


def test_a_plan_is_frozen() -> None:
    plan = plan_request(lending(), request("npf_ratio"))
    with pytest.raises(AttributeError):
        plan.limit = 5  # type: ignore[misc]


def test_planning_does_not_change_the_model_or_the_request() -> None:
    model = Model.build(lending().entities.values(), lending().metrics.values())
    req = request("npf_ratio", dimensions=["branch.region_name"])
    before = (dict(model.entities), dict(model.metrics), req.model_dump())
    plan_request(model, req)
    assert (dict(model.entities), dict(model.metrics), req.model_dump()) == before


def test_model_snapshots_of_the_same_objects_are_equal() -> None:
    a = Model.build(lending().entities.values(), lending().metrics.values())
    b = Model.build(lending().entities.values(), lending().metrics.values())
    assert a == b
