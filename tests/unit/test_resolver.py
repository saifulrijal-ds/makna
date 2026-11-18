"""The rules of ARCHITECTURE 8.4 on fabricated search results and the sample model: no database,
no embedding model (WP-20 acceptance 2 to 4). The numbers on the real index are the evals'."""

from __future__ import annotations

import datetime

import pytest

from makna.model.governance import STRICT
from makna.model.request import QueryRequest, RequestFilter
from makna.resolve import Resolution, resolve
from makna.search.confidence import (
    AMBIGUITY_COVERAGE_GAP,
    AMBIGUITY_SCORE_RATIO,
    Band,
    Confidence,
    is_ambiguous,
)
from makna.search.hybrid import Hit, SearchResult
from tests.unit.compiler_support import lending

TODAY = datetime.date(2026, 7, 1)


def hit(
    object_id: str,
    score: float,
    coverage: float,
    matched: tuple[str, ...] = (),
    *,
    names: str | None = None,
    pre: float | None = None,
) -> Hit:
    kind = object_id.partition(":")[0]
    return Hit(
        object_id=object_id,
        kind=kind,
        status="approved",
        domain="lending",
        names=names or object_id.partition(":")[2].replace("_", " "),
        description=f"What {object_id} means.",
        score=score,
        keyword_score=None,
        keyword_rank=None,
        vector_similarity=None,
        vector_rank=None,
        matched_terms=matched,
        coverage=coverage,
        pre_fusion_score=score if pre is None else pre,
    )


def found(*hits: Hit, band: Band = "high") -> SearchResult:
    confidence = Confidence(band, 1.0, (), (), None)
    return SearchResult("question", hits, confidence)


def run(
    question: str,
    *hits: Hit,
    band: Band = "high",
    today: datetime.date = TODAY,
    glossary_related: dict[str, list[str]] | None = None,
) -> Resolution:
    return resolve(
        question,
        today,
        found=found(*hits, band=band),
        model=lending(),
        policy=STRICT,
        glossary_related=glossary_related,
    )


def clear(metric: str, matched: tuple[str, ...] = ()) -> tuple[Hit, Hit]:
    """A first metric hit that is well ahead of the second in coverage."""
    return (
        hit(f"metric:{metric}", 0.0184, 1.0, matched),
        hit("metric:collection_action_count", 0.0179, 0.2),
    )


# --- the ambiguity rule ---------------------------------------------------------------------


def test_the_two_constants_are_the_ones_of_the_architecture() -> None:
    assert AMBIGUITY_SCORE_RATIO == 0.85
    assert AMBIGUITY_COVERAGE_GAP == 0.15


@pytest.mark.parametrize(
    ("second_score", "second_coverage", "expected"),
    [
        (0.0180, 1.0, True),  # close in both
        (0.0180, 0.86, True),  # a gap just under 0.15
        (0.0180, 0.84, False),  # a gap of 0.16
        (0.0180, 0.85, False),  # exactly 0.15 is not "less than"
        (0.0157, 1.0, True),  # a little over 0.85 of the first score
        (0.0156, 1.0, False),  # a little under it
        (0.0150, 1.0, False),  # score below 0.85 of the first
    ],
)
def test_ambiguous_needs_a_close_score_and_a_close_coverage(
    second_score: float, second_coverage: float, expected: bool
) -> None:
    assert is_ambiguous(0.0184, 1.0, second_score, second_coverage) is expected


def test_the_ratio_is_taken_on_the_pre_fusion_score_not_the_fused_one() -> None:
    # D-38: the fused scores of neighbouring ranks are within 3 percent whatever the question
    # says, so the test would always be met. The two legs before fusion tell the metrics apart.
    far = run(
        "disbursement",
        hit("metric:disbursement_amount", 0.01844, 1.0, ("disbursement",), pre=0.50),
        hit("metric:disbursed_contract_count", 0.01815, 1.0, ("disbursement",), pre=0.40),
    )
    assert far.status == "resolved" and far.ambiguous_between == ()
    assert far.request is not None and far.request.metric == "disbursement_amount"
    near = run(
        "disbursement",
        hit("metric:disbursement_amount", 0.01844, 1.0, ("disbursement",), pre=0.50),
        hit("metric:disbursed_contract_count", 0.01815, 1.0, ("disbursement",), pre=0.45),
    )
    assert near.status == "ambiguous"
    assert near.ambiguous_between == (
        "metric:disbursement_amount",
        "metric:disbursed_contract_count",
    )


def test_a_fused_score_far_apart_does_not_decide_when_the_legs_are_close() -> None:
    result = run(
        "disbursement",
        hit("metric:disbursement_amount", 0.0300, 1.0, ("disbursement",), pre=0.50),
        hit("metric:disbursed_contract_count", 0.0100, 1.0, ("disbursement",), pre=0.49),
    )
    assert result.status == "ambiguous"


def test_two_close_candidates_are_ambiguous_and_nothing_is_chosen() -> None:
    result = run(
        "Berapa NPF kita?",
        hit("metric:npf_ratio", 0.01841, 1.0, ("npf",)),
        hit("metric:npf_outstanding_principal", 0.01818, 1.0, ("npf",)),
        hit("glossary_term:npf", 0.01714, 1.0, ("npf",)),
    )
    assert result.status == "ambiguous"
    assert result.request is None
    assert result.ambiguous_between == ("metric:npf_ratio", "metric:npf_outstanding_principal")
    assert [c.id for c in result.candidates] == [
        "metric:npf_ratio",
        "metric:npf_outstanding_principal",
    ]
    assert result.chosen is None


def test_a_wide_coverage_gap_picks_the_first_metric() -> None:
    result = run(
        "What is our NPF ratio right now?",
        hit("metric:npf_ratio", 0.01844, 0.9, ("npf", "ratio")),
        hit("metric:npf_outstanding_principal", 0.01815, 0.5, ("npf",)),
    )
    assert result.status == "resolved"
    assert result.request == QueryRequest(metric="npf_ratio")
    assert result.ambiguous_between == ()
    assert result.unresolved_terms == ("right", "now")


def test_a_second_candidate_far_behind_in_score_is_not_ambiguous() -> None:
    result = run(
        "Berapa NPF kita?",
        hit("metric:npf_ratio", 0.0300, 1.0, ("npf",), pre=0.30),
        hit("metric:npf_outstanding_principal", 0.0200, 1.0, ("npf",), pre=0.20),
    )
    assert result.status == "resolved"


def test_a_low_confidence_search_is_no_match_and_still_lists_what_it_found() -> None:
    result = run(
        "What is our net interest margin this year?",
        hit("metric:collected_amount", 0.018, 0.0),
        band="low",
    )
    assert result.status == "no_match"
    assert result.request is None
    assert [h.id for h in result.hits] == ["metric:collected_amount"]
    assert result.unresolved_terms == ("net", "interest", "margin")
    assert any("do not use a different metric" in note for note in result.notes)
    # The period is still read, so the bench shows what was understood.
    assert [i.kind for i in result.interpretation] == ["time"]


def test_no_metric_hit_at_all_is_no_match() -> None:
    result = run("NPF", hit("entity:contract", 0.018, 1.0), band="high")
    assert result.status == "no_match"


def test_a_draft_is_never_a_candidate() -> None:
    # write_off_amount is a draft in the sample model: a search that returned it is not trusted.
    result = run("write off", hit("metric:write_off_amount", 0.018, 1.0, ("write",)))
    assert result.status == "no_match"
    assert result.candidates == ()


# --- the pieces of the request --------------------------------------------------------------


def test_a_full_request_is_built_from_the_words() -> None:
    result = run(
        "Show the monthly NPF ratio trend for car-backed financing in the first half of 2026.",
        *clear("npf_ratio", ("npf", "ratio", "financing")),
    )
    assert result.status == "resolved"
    assert result.request == QueryRequest(
        metric="npf_ratio",
        filters=(RequestFilter(dimension="product.product_category", op="=", value="car_backed"),),
        grain="month",
        time_start="2026-01-01",
        time_end="2026-07-01",
    )
    assert {i.kind for i in result.interpretation} == {"time", "grain", "filter"}
    assert result.unresolved_terms == ()


@pytest.mark.parametrize(
    ("question", "dimensions"),
    [
        ("Disbursement by branch last month", ["branch.branch_name"]),
        ("Total pencairan tahun ini untuk tiap wilayah?", ["branch.region_name"]),
        ("disbursement for each region", ["branch.region_name"]),
        ("disbursement per kategori produk", ["product.product_category"]),
        ("disbursement by regions", ["branch.region_name"]),
        (
            "approval rate by risk grade and sales channel",
            ["application.risk_grade", "application.channel"],
        ),
        (
            "disbursement by region, product category",
            ["branch.region_name", "product.product_category"],
        ),
        ("disbursement", []),
    ],
)
def test_dimensions_come_after_a_cue(question: str, dimensions: list[str]) -> None:
    metric = "approval_rate" if "approval" in question else "disbursement_amount"
    result = run(question, *clear(metric))
    assert result.status == "resolved"
    assert result.request is not None
    assert list(result.request.dimensions) == dimensions


def test_a_dimension_word_without_a_cue_is_not_a_dimension() -> None:
    result = run("disbursement of each product in this region", *clear("disbursement_amount"))
    assert result.request is not None
    # "each product" is a dimension; "this region" has no cue.
    assert list(result.request.dimensions) == ["product.product_name"]
    assert "region" in result.unresolved_terms


def test_a_phrase_that_two_dimensions_carry_is_not_guessed() -> None:
    result = run("disbursement by provinsi", *clear("disbursement_amount"))
    assert result.request is not None and result.request.dimensions == ()
    assert any("provinsi" in n and "could mean" in n for n in result.notes)
    assert "provinsi" in result.unresolved_terms


def test_a_dimension_the_metric_cannot_be_cut_by_is_said_and_left_out() -> None:
    result = run("NPF ratio by payment channel", *clear("npf_ratio", ("npf", "ratio")))
    assert result.request == QueryRequest(metric="npf_ratio")
    assert any("payment.payment_channel" in n and "cannot be cut by" in n for n in result.notes)
    assert result.unresolved_terms == ("payment", "channel")


def test_a_personal_dimension_is_never_matched() -> None:
    result = run("disbursement by customer name", *clear("disbursement_amount"))
    assert result.request is not None and result.request.dimensions == ()
    result = run("disbursement by tahun lahir", *clear("disbursement_amount"))
    assert result.request is not None and result.request.dimensions == ()


@pytest.mark.parametrize(
    ("question", "dimension", "value"),
    [
        ("Outstanding for Jawa Barat", "branch.region_name", "Jawa Barat"),
        ("collection rate for produk syariah", "product.financing_scheme", "sharia"),
        ("disbursement to unsecured products", "product.product_category", "unsecured"),
        ("disbursement of used car purchase", "product.product_category", "used_car_purchase"),
        ("disbursement for konvensional", "product.financing_scheme", "conventional"),
    ],
)
def test_a_declared_value_becomes_an_equality_filter(
    question: str, dimension: str, value: str
) -> None:
    result = run(question, *clear("disbursement_amount"))
    assert result.request is not None
    assert result.request.filters == (RequestFilter(dimension=dimension, op="=", value=value),)


def test_a_word_the_metric_name_matched_is_not_also_a_value() -> None:
    # "approved" is a declared value of application.decision_status, but the metric says it.
    result = run(
        "approved applications",
        hit("metric:approved_application_count", 0.0184, 1.0, ("approved", "applications")),
        hit("metric:collection_action_count", 0.0179, 0.2),
    )
    assert result.request == QueryRequest(metric="approved_application_count")


def test_a_value_of_fewer_than_three_characters_is_never_matched() -> None:
    result = run("approval rate for grade a and b", *clear("approval_rate"))
    assert result.request is not None and result.request.filters == ()


def test_words_that_match_nothing_are_returned_in_order_without_repeats() -> None:
    result = run(
        "disbursement zorp last month zorp flurble the of",
        *clear("disbursement_amount", ("disbursement",)),
    )
    assert result.unresolved_terms == ("zorp", "flurble")


def test_the_time_range_is_inclusive_to_exclusive() -> None:
    result = run("disbursement in June 2026", *clear("disbursement_amount"))
    assert result.request is not None
    assert (result.request.time_start, result.request.time_end) == ("2026-06-01", "2026-07-01")


def test_a_question_without_a_time_phrase_has_no_range() -> None:
    result = run("What is our NPF ratio", *clear("npf_ratio"))
    assert result.request is not None
    assert (result.request.time_start, result.request.time_end) == (None, None)


def test_only_the_first_time_phrase_is_used_and_the_note_says_so() -> None:
    result = run("disbursement Q1 2026 and Q2 2026", *clear("disbursement_amount"))
    assert result.request is not None
    assert result.request.time_start == "2026-01-01"
    assert any("Q2 2026".lower() in n for n in result.notes)


def test_trend_uses_the_default_grain_of_the_metric() -> None:
    metric = lending().metrics["npf_ratio"]
    assert metric.default_grain is not None
    result = run("npf ratio trend", *clear("npf_ratio", ("npf", "ratio")))
    assert result.request is not None and result.request.grain == metric.default_grain


# --- glossary terms -------------------------------------------------------------------------


def test_a_glossary_term_that_explains_the_question_better_adds_its_related_metrics() -> None:
    result = run(
        "restructuring",
        hit("glossary_term:restructuring", 0.0180, 1.0, ("restructuring",)),
        hit("metric:disbursement_amount", 0.0170, 0.0),
        glossary_related={
            "glossary_term:restructuring": [
                "metric:restructured_outstanding_principal",
                "metric:restructured_ratio",
                "entity:installment",
            ]
        },
    )
    assert [c.id for c in result.candidates][:2] == [
        "metric:restructured_outstanding_principal",
        "metric:restructured_ratio",
    ]
    assert {c.via for c in result.candidates[:2]} == {"glossary_term:restructuring"}
    assert result.status == "ambiguous"


def test_a_glossary_term_that_explains_no_better_than_a_metric_adds_nothing() -> None:
    result = run(
        "Disbursement by branch",
        hit("metric:disbursement_amount", 0.0184, 1.0, ("disbursement",)),
        hit("glossary_term:disbursement", 0.0170, 1.0, ("disbursement",)),
        glossary_related={
            "glossary_term:disbursement": [
                "metric:disbursement_amount",
                "metric:disbursed_contract_count",
            ]
        },
    )
    assert [c.id for c in result.candidates] == ["metric:disbursement_amount"]
    assert result.status == "resolved"


# --- what is in the answer ------------------------------------------------------------------


def test_the_hits_carry_scores_coverage_and_labels() -> None:
    result = run("NPF ratio", *clear("npf_ratio", ("npf", "ratio")))
    first = result.hits[0]
    assert (first.id, first.label, first.kind) == ("metric:npf_ratio", "NPF Ratio", "metric")
    assert first.score == 0.0184 and first.coverage == 1.0
    assert first.matched_terms == ("npf", "ratio")
    assert result.candidates[0].description == "What metric:npf_ratio means."
    assert result.today == TODAY


def test_the_same_question_and_date_give_the_same_resolution_whatever_the_clock_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = "Disbursement by branch last month and this year"
    before = run(question, *clear("disbursement_amount"))

    class Frozen(datetime.date):
        @classmethod
        def today(cls) -> Frozen:
            return cls(1999, 12, 31)

    monkeypatch.setattr(datetime, "date", Frozen)
    after = run(question, *clear("disbursement_amount"))
    assert after == before
    assert after.request is not None and after.request.time_start == "2026-06-01"


def test_a_datetime_for_today_is_read_as_its_date() -> None:
    result = run(
        "disbursement last month",
        *clear("disbursement_amount"),
        today=datetime.datetime(2026, 7, 1, 13, 30),
    )
    assert result.today == TODAY
    assert result.request is not None and result.request.time_end == "2026-07-01"


def test_ambiguous_pair_reads_the_pre_fusion_score_of_the_hits() -> None:
    from makna.search.hybrid import ambiguous_pair

    def pair(second_pre: float) -> tuple[str, ...]:
        hits = (
            hit("metric:a", 0.0184, 1.0, pre=0.50),
            hit("metric:b", 0.0182, 1.0, pre=second_pre),
        )
        return ambiguous_pair(hits, "high")

    assert pair(0.45) == ("metric:a", "metric:b")
    assert pair(0.40) == ()  # fused scores 1% apart, legs 20% apart
    assert ambiguous_pair((hit("metric:a", 0.0184, 1.0, pre=0.5),), "high") == ()
    assert (
        ambiguous_pair(
            (hit("metric:a", 0.02, 1.0, pre=0.5), hit("metric:b", 0.02, 1.0, pre=0.5)), "low"
        )
        == ()
    )


def test_the_boundaries_are_strict_for_the_gap_and_inclusive_for_the_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Values that are exact in binary, so that a `<` turned into `<=` (or `>=` into `>`) shows.
    from makna.search import confidence

    monkeypatch.setattr(confidence, "AMBIGUITY_COVERAGE_GAP", 0.25)
    monkeypatch.setattr(confidence, "AMBIGUITY_SCORE_RATIO", 0.5)
    assert is_ambiguous(1.0, 1.0, 0.5, 0.8125) is True  # ratio exactly 0.5; gap 0.1875
    assert is_ambiguous(1.0, 1.0, 0.5, 0.75) is False  # a gap of exactly 0.25 is not "less than"
    assert is_ambiguous(1.0, 0.75, 0.5, 1.0) is False  # the same, the other way round
    assert is_ambiguous(1.0, 1.0, 0.4999, 1.0) is False  # a ratio below 0.5


@pytest.mark.parametrize(
    ("question", "phrase", "ref"),
    [
        ("disbursement by customer name", "customer name", "customer.full_name"),
        ("disbursement by tahun lahir", "tahun lahir", "customer.birth_year"),
        ("disbursement per nomor telepon", "nomor telepon", "customer.phone_number"),
    ],
)
def test_a_personal_dimension_that_is_asked_for_is_left_out_with_a_note(
    question: str, phrase: str, ref: str
) -> None:
    result = run(question, *clear("disbursement_amount"))
    assert result.request is not None and result.request.dimensions == ()
    assert any(phrase in n and ref in n and "personal data" in n for n in result.notes), (
        result.notes
    )
