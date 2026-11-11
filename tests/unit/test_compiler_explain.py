"""WP-09 criterion 8: the explanation (ARCHITECTURE 7.8, QRY-3)."""

from __future__ import annotations

from makna.compiler.compile import compile_request
from makna.compiler.explain import explain
from makna.compiler.plan import plan_request

from .compiler_support import lending, request

COLLECTION_BY_REGION = {
    "dimensions": ["branch.region_name"],
    "time_start": "2026-01-01",
    "time_end": "2026-07-01",
}


def positions(sentences: tuple[str, ...], *needles: str) -> list[int]:
    """Index of the first sentence containing each needle, failing if one is missing."""
    found = []
    for needle in needles:
        hits = [i for i, text in enumerate(sentences) if needle in text]
        assert hits, f"no sentence mentions {needle!r}: {sentences}"
        found.append(hits[0])
    return found


def test_collection_rate_by_region_explains_formula_time_filters_and_join_path_in_order() -> None:
    response = compile_request(lending(), request("collection_rate", **COLLECTION_BY_REGION))
    text = response.explanation
    formula, collected_time, due_time, collected_filter, due_filter, join = positions(
        text,
        "Collection Rate = Amount Collected divided by Amount Due",
        "Amount Collected: Payment rows are placed by posting date",
        "Amount Due: Scheduled Installment rows are placed by due date",
        "NOT is_reversed",
        "is_current",
        "contract.servicing_branch",
    )
    # formula, then each input's time dimension and range, then each definition filter, then joins
    assert formula < min(collected_time, due_time)
    assert max(collected_time, due_time) < min(collected_filter, due_filter)
    assert max(collected_filter, due_filter) < join
    assert "from 2026-01-01 up to but not including 2026-07-01" in text[collected_time]
    assert "from 2026-01-01 up to but not including 2026-07-01" in text[due_time]
    assert "posting_date" in text[collected_time] and "due_date" in text[due_time]
    # The join path to branch is spelled out for each input, from its own table.
    assert "payment > contract > branch" in " ".join(text)
    assert "installment > contract > branch" in " ".join(text)
    assert "(part of the measure definition)" in text[collected_filter]
    assert response.warnings == ()


def test_the_formula_says_how_the_sides_are_combined() -> None:
    text = compile_request(
        lending(), request("collection_rate", **COLLECTION_BY_REGION)
    ).explanation
    assert "calculated separately and then combined by" in text[0]
    assert "region" in text[0]
    bare = compile_request(lending(), request("collection_rate")).explanation
    assert "combined into one value" in bare[0]


def test_a_balance_metric_adds_the_balance_warning() -> None:
    quarterly = compile_request(
        lending(), request("outstanding_principal", grain="quarter", **COLLECTION_BY_REGION)
    )
    assert len(quarterly.warnings) == 1
    assert quarterly.warnings[0].startswith("Outstanding Principal is a balance")
    assert "each quarter" in quarterly.warnings[0] and "last snapshot date" in quarterly.warnings[0]
    plain = compile_request(lending(), request("outstanding_principal"))
    assert "latest snapshot date in the time range" in plain.warnings[0]
    # A ratio of two balances warns once for each balance.
    ratio = compile_request(lending(), request("npf_ratio", grain="month"))
    assert [w.split(" is a balance")[0] for w in ratio.warnings] == [
        "NPF Outstanding Principal",
        "Outstanding Principal",
    ]


def test_a_flow_metric_has_no_balance_warning() -> None:
    assert compile_request(lending(), request("disbursement_amount")).warnings == ()


def test_rules_and_request_filters_are_explained() -> None:
    response = compile_request(
        lending(),
        request(
            "restructured_outstanding_principal",
            filters=[
                {"dimension": "product.product_category", "op": "in", "value": ["car_backed"]}
            ],
        ),
    )
    text = " ".join(response.explanation)
    assert "the rule contract.restructured holds" in text
    assert (
        "The request keeps only rows where product.product_category is one of 'car_backed'" in text
    )
    rule_at, filter_at = positions(
        response.explanation, "the rule contract.restructured", "The request keeps only"
    )
    assert rule_at < filter_at


def test_the_explanation_is_a_pure_function_of_the_plan() -> None:
    model = lending()
    plan = plan_request(model, request("collection_rate", **COLLECTION_BY_REGION))
    assert explain(model, plan) == explain(model, plan)
