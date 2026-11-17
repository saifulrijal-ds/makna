"""WP-09 criterion 8: the explanation (ARCHITECTURE 7.8, QRY-3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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


def test_join_sentences_start_with_a_capital_letter() -> None:
    response = compile_request(lending(), request("collection_rate", **COLLECTION_BY_REGION))
    joins = [t for t in response.explanation if " are joined " in t or "rows are joined" in t]
    assert joins and all(t[0].isupper() for t in joins), joins
    assert all(t[0].isupper() for t in response.explanation)


def test_a_rule_of_the_definition_on_another_entity_gets_a_join_sentence() -> None:
    response = compile_request(lending(), request("restructured_outstanding_principal"))
    joins = [t for t in response.explanation if "rows are joined" in t]
    assert len(joins) == 1
    assert joins[0].startswith("Rule contract.restructured comes from Financing Contract")
    assert "contract_snapshot > contract through contract_snapshot.contract" in joins[0]
    # It follows the definition-filter sentence and precedes nothing else.
    filter_at, join_at = positions(
        response.explanation, "the rule contract.restructured holds", "rows are joined"
    )
    assert filter_at < join_at


def limit_sentences(sentences: tuple[str, ...]) -> list[str]:
    return [text for text in sentences if "is cut at" in text]


def test_a_request_with_a_dimension_says_the_result_is_cut_at_the_limit_in_key_order() -> None:
    response = compile_request(lending(), request("collection_rate", **COLLECTION_BY_REGION))
    [sentence] = limit_sentences(response.explanation)
    assert sentence == (
        "The result is cut at 1000 rows: the rows are sorted in ascending order by region name, "
        "and "
        "only the first 1000 are returned, so a result of exactly 1000 rows may be incomplete."
    )
    assert response.explanation[-1] == sentence
    # The SQL orders by the dimension and then cuts at the limit, as the sentence says.
    assert "ORDER BY" in response.sql and response.sql.endswith("LIMIT 1000")


def test_a_grain_alone_gets_the_limit_sentence_naming_the_period() -> None:
    response = compile_request(
        lending(), request("collection_rate", grain="month", **{"time_start": "2026-01-01"})
    )
    [sentence] = limit_sentences(response.explanation)
    assert "sorted in ascending order by month, and only the first 1000" in sentence


def test_a_request_with_no_dimension_and_no_grain_gets_no_limit_sentence() -> None:
    response = compile_request(lending(), request("collection_rate"))
    assert limit_sentences(response.explanation) == []


def test_the_limit_sentence_states_the_requested_limit_and_its_keys_in_request_order() -> None:
    response = compile_request(
        lending(),
        request(
            "collection_rate",
            grain="month",
            dimensions=["branch.region_name", "branch.branch_name"],
            limit=50,
        ),
    )
    [sentence] = limit_sentences(response.explanation)
    assert sentence.startswith("The result is cut at 50 rows")
    assert "sorted in ascending order by month, region name and branch name" in sentence
    assert "exactly 50 rows may be incomplete" in sentence
    assert "LIMIT 50" in response.sql


def test_the_limit_is_always_a_whole_number_so_the_sentence_always_has_one() -> None:
    # An omitted limit is the default of 1000; an explicit null is refused at the boundary.
    assert request("collection_rate").limit == 1000
    with pytest.raises(ValidationError):
        request("collection_rate", limit=None)
