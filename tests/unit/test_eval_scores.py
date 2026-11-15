"""WP-15: score arithmetic on small hand-made rankings (docs/EVALS.md section 3)."""

from __future__ import annotations

import pytest

from evals.scores import (
    LEG_SCORES,
    RetrievalRecord,
    distinct_metrics,
    latency_summary,
    percentile,
    recall_hit,
    reciprocal_rank,
    score_all,
    score_confidence,
    score_system,
)


def answer(
    id: str, expected: str, ranked: list[str], *, language: str = "en", band: str = "high"
) -> RetrievalRecord:
    return RetrievalRecord(
        id=id,
        language=language,
        outcome="answer",
        reason=None,
        expected_metrics=(expected,),
        rankings={"fused": ranked, "keyword": ranked, "vector": ranked},
        confidence=band,
    )


def clarify(id: str, candidates: tuple[str, ...], ranked: list[str]) -> RetrievalRecord:
    return RetrievalRecord(
        id=id,
        language="en",
        outcome="clarify",
        reason=None,
        candidates=candidates,
        rankings={"fused": ranked, "keyword": ranked, "vector": ranked},
        confidence="high",
    )


def test_recall_at_3_of_an_answer_counts_any_expected_metric() -> None:
    record = RetrievalRecord("G1", "en", "answer", None, expected_metrics=("a", "b"))
    assert recall_hit(record, ["x", "y", "b", "a"]) is True
    assert recall_hit(record, ["x", "y", "z", "a"]) is False


def test_recall_at_3_of_a_clarify_question_needs_all_candidates() -> None:
    record = clarify("G2", ("a", "b"), [])
    assert recall_hit(record, ["a", "x", "b"]) is True
    assert recall_hit(record, ["a", "x", "y", "b"]) is False
    assert recall_hit(record, ["a", "x", "y"]) is False


def test_other_outcomes_are_not_part_of_recall() -> None:
    assert recall_hit(RetrievalRecord("G3", "en", "no_metric", "no_definition"), ["a"]) is None
    assert recall_hit(RetrievalRecord("G4", "en", "refuse", "personal_data"), ["a"]) is None


def test_reciprocal_rank_is_one_over_the_first_correct_rank() -> None:
    record = RetrievalRecord("G1", "en", "answer", None, expected_metrics=("m",))
    assert reciprocal_rank(record, ["m"]) == 1.0
    assert reciprocal_rank(record, ["x", "m"]) == 0.5
    assert reciprocal_rank(record, ["x", "y", "z", "m"]) == 0.25


def test_reciprocal_rank_is_zero_beyond_rank_10() -> None:
    record = RetrievalRecord("G1", "en", "answer", None, expected_metrics=("m",))
    ranked = [f"x{i}" for i in range(10)] + ["m"]
    assert reciprocal_rank(record, ranked[:10] + ["m"]) == 0.0
    assert reciprocal_rank(record, ranked[:9] + ["m"]) == pytest.approx(0.1)
    assert reciprocal_rank(RetrievalRecord("G2", "en", "clarify", None), ["m"]) is None


def test_glossary_terms_expand_to_their_related_metrics_at_their_rank() -> None:
    related = {
        "glossary_term:npf": ["metric:npf_ratio", "metric:npf_outstanding_principal", "entity:x"],
    }
    ids = ["metric:a", "glossary_term:npf", "metric:npf_ratio", "dimension:x.y", "metric:b"]
    assert distinct_metrics(ids, related) == ["a", "npf_ratio", "npf_outstanding_principal", "b"]


def test_a_term_with_no_related_metrics_contributes_nothing() -> None:
    assert distinct_metrics(["glossary_term:tenor", "metric:a"], {}) == ["a"]


def test_the_expansion_can_make_a_clarify_question_found() -> None:
    record = clarify("G16", ("npf_ratio", "npf_outstanding_principal"), [])
    ranked = distinct_metrics(
        ["glossary_term:npf"],
        {"glossary_term:npf": ["metric:npf_ratio", "metric:npf_outstanding_principal"]},
    )
    assert recall_hit(record, ranked) is True


def test_system_scores_over_a_small_set() -> None:
    records = [
        answer("G1", "a", ["a", "x"]),  # rank 1
        answer("G2", "b", ["x", "b"]),  # rank 2
        answer("G3", "c", ["x", "y", "z", "c"]),  # rank 4: not in the top 3
        clarify("G4", ("d", "e"), ["d", "e", "x"]),
        RetrievalRecord("G5", "en", "no_metric", "no_definition", rankings={"fused": ["a"]}),
    ]
    scores = score_system(records, "fused")
    assert scores["n_recall_at_3"] == 4 and scores["n_answer"] == 3
    assert scores["recall_at_3"] == pytest.approx(3 / 4)
    assert scores["recall_at_1"] == pytest.approx(1 / 3)
    assert scores["mrr"] == pytest.approx((1 + 0.5 + 0.25) / 3)


def test_confidence_scores() -> None:
    records = [
        answer("G1", "a", ["a"], band="low"),
        answer("G2", "b", ["b"], band="high"),
        answer("G3", "c", ["c"], band="medium"),
        answer("G4", "d", ["d"], band="high"),
        RetrievalRecord("G5", "en", "no_metric", "no_definition", confidence="low"),
        RetrievalRecord("G6", "en", "no_metric", "no_definition", confidence="medium"),
        RetrievalRecord("G7", "en", "no_metric", "dimension_not_available", confidence="medium"),
        # A clarify question with low confidence is not an `answer`: it is not a false abstention.
        RetrievalRecord("G8", "en", "clarify", None, confidence="low"),
    ]
    scores = score_confidence(records)
    assert scores["false_abstention_rate"] == 0.25 and scores["n_answer_confidence"] == 4
    assert scores["abstention_recall"] == 0.5 and scores["n_no_definition"] == 2


def test_an_empty_set_has_no_score() -> None:
    scores = score_system([], "fused")
    assert scores["recall_at_3"] is None and scores["mrr"] is None


def test_scores_are_split_by_language_and_by_system() -> None:
    records = [
        answer("G1", "a", ["a"], language="en"),
        answer("G2", "b", ["x", "y", "z", "b"], language="id"),
        answer("G3", "c", ["c"], language="mixed"),
    ]
    scores = score_all(records)
    fused = scores["fused"]
    assert fused["by_language"]["en"]["recall_at_3"] == 1.0
    assert fused["by_language"]["id"]["recall_at_3"] == 0.0
    assert fused["by_language"]["mixed"]["n_recall_at_3"] == 1
    for system in ("fused", "keyword", "vector"):
        assert set(LEG_SCORES) <= set(scores[system])
    assert "false_abstention_rate" in fused["by_language"]["en"]
    assert "false_abstention_rate" not in scores["keyword"]


def test_percentile_is_the_nearest_rank() -> None:
    values = [float(i) for i in range(1, 101)]
    assert percentile(values, 95) == 95.0
    assert percentile(values, 50) == 50.0
    assert percentile([5.0], 95) == 5.0
    assert percentile([], 95) is None
    assert percentile([3.0, 1.0, 2.0], 100) == 3.0


def test_latency_summary() -> None:
    summary = latency_summary([10.0, 20.0, 30.0, 400.0])
    assert summary == {"n": 4, "p50_ms": 20.0, "p95_ms": 400.0, "max_ms": 400.0}
