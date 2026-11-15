"""Retrieval scores (docs/EVALS.md section 3): plain arithmetic on ranked lists, no database.

A search returns ranked object ids. A glossary term counts as its `related` metrics, placed at the
term's rank, and the ranking that is scored is the list of **distinct metrics** that results in.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

RECALL_K = 3
MRR_DEPTH = 10
"""A correct metric beyond this rank scores 0 in the mean reciprocal rank."""

SYSTEMS = ("fused", "keyword", "vector")
LANGUAGES = ("en", "id", "mixed")

LEG_SCORES = ("recall_at_3", "recall_at_1", "mrr")
"""Scores of every system (fused and each leg alone)."""
CONFIDENCE_SCORES = ("abstention_recall", "false_abstention_rate")
"""Scores of the fused search only: a confidence band is the fused search's."""


@dataclass(frozen=True)
class RetrievalRecord:
    """What one question needs for scoring."""

    id: str
    language: str
    outcome: str
    reason: str | None
    expected_metrics: tuple[str, ...] = ()
    """`answer`: any of these is correct."""
    candidates: tuple[str, ...] = ()
    """`clarify`: all of these must be found."""
    rankings: Mapping[str, Sequence[str]] = field(default_factory=dict)
    """System name -> distinct metric names, best first."""
    confidence: str | None = None
    """The band of the fused search."""


def distinct_metrics(object_ids: Sequence[str], related: Mapping[str, Sequence[str]]) -> list[str]:
    """Metric names in rank order, each once. `related` maps a `glossary_term:` id to the
    `metric:` ids it relates to; a term's metrics take the term's place, in their listed order."""
    seen: set[str] = set()
    ranked: list[str] = []
    for object_id in object_ids:
        if object_id.startswith("metric:"):
            names = [object_id]
        elif object_id.startswith("glossary_term:"):
            names = [r for r in related.get(object_id, ()) if r.startswith("metric:")]
        else:
            names = []
        for full in names:
            name = full.partition(":")[2]
            if name not in seen:
                seen.add(name)
                ranked.append(name)
    return ranked


def recall_hit(record: RetrievalRecord, ranked: Sequence[str], k: int = RECALL_K) -> bool | None:
    """Whether the question counts as found in the first `k`. `answer`: any expected metric.
    `clarify`: every candidate. None for other outcomes (not part of recall)."""
    top = set(ranked[:k])
    if record.outcome == "answer":
        return bool(top & set(record.expected_metrics))
    if record.outcome == "clarify":
        return bool(record.candidates) and set(record.candidates) <= top
    return None


def reciprocal_rank(record: RetrievalRecord, ranked: Sequence[str]) -> float | None:
    """1 / rank of the first correct metric; 0 when it is not in the first 10. `answer` only."""
    if record.outcome != "answer":
        return None
    correct = set(record.expected_metrics)
    for rank, name in enumerate(ranked[:MRR_DEPTH], start=1):
        if name in correct:
            return 1.0 / rank
    return 0.0


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def score_system(records: Sequence[RetrievalRecord], system: str) -> dict[str, float | int | None]:
    """Recall at 3 (`answer` and `clarify`), recall at 1 and mean reciprocal rank (`answer`),
    each with the number of questions it is over."""
    at3: list[float] = []
    at1: list[float] = []
    rr: list[float] = []
    for record in records:
        ranked = record.rankings.get(system, ())
        found = recall_hit(record, ranked)
        if found is not None:
            at3.append(float(found))
        if record.outcome == "answer":
            at1.append(float(bool(recall_hit(record, ranked, 1))))
            value = reciprocal_rank(record, ranked)
            assert value is not None
            rr.append(value)
    return {
        "recall_at_3": _mean(at3),
        "n_recall_at_3": len(at3),
        "recall_at_1": _mean(at1),
        "mrr": _mean(rr),
        "n_answer": len(at1),
    }


def score_confidence(records: Sequence[RetrievalRecord]) -> dict[str, float | int | None]:
    """Abstention recall (`no_metric` with reason `no_definition`: share with confidence `low`)
    and false abstention rate (`answer`: share with confidence `low`)."""
    abstained = [
        float(r.confidence == "low")
        for r in records
        if r.outcome == "no_metric" and r.reason == "no_definition"
    ]
    false = [float(r.confidence == "low") for r in records if r.outcome == "answer"]
    return {
        "abstention_recall": _mean(abstained),
        "n_no_definition": len(abstained),
        "false_abstention_rate": _mean(false),
        "n_answer_confidence": len(false),
    }


def score_all(records: Sequence[RetrievalRecord]) -> dict[str, Any]:
    """Every retrieval score: each system overall and by language, and the confidence scores."""
    scores: dict[str, Any] = {}
    for system in SYSTEMS:
        entry: dict[str, Any] = dict(score_system(records, system))
        if system == "fused":
            entry.update(score_confidence(records))
        by_language: dict[str, Any] = {}
        for language in LANGUAGES:
            subset = [r for r in records if r.language == language]
            part: dict[str, Any] = dict(score_system(subset, system))
            if system == "fused":
                part.update(score_confidence(subset))
            by_language[language] = part
        entry["by_language"] = by_language
        scores[system] = entry
    return scores


def percentile(values: Sequence[float], q: float) -> float | None:
    """The nearest-rank percentile (`q` in 0..100): the smallest value with at least `q` percent
    of the values at or below it. None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def latency_summary(times_ms: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "n": len(times_ms),
        "p50_ms": percentile(times_ms, 50),
        "p95_ms": percentile(times_ms, 95),
        "max_ms": max(times_ms) if times_ms else None,
    }
