"""The held-out retrieval split (EVL-2, docs/EVALS.md section 3.2).

The 46 questions of `evals/golden/heldout_questions.yaml` expect one metric each. They are
searched as the gated golden questions are (kinds `metric` and `glossary_term`, a viewer, limit
10, a glossary term counting as its related metrics) and scored with the same arithmetic, so the
two splits sit side by side. Nothing here changes a setting; the tuning code cannot import this
module (`tests/unit/test_eval_heldout.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Engine

from evals.golden.heldout import HeldoutFile, HeldoutQuestion
from evals.retrieval import KINDS, LIMIT, ROLE, Searchers, rank_systems
from evals.scores import (
    RECALL_K,
    SYSTEMS,
    RetrievalRecord,
    recall_hit,
    reciprocal_rank,
    score_all,
)

BANDS = ("high", "medium", "low")


def _record(
    question: HeldoutQuestion, rankings: dict[str, list[str]], band: str
) -> RetrievalRecord:
    return RetrievalRecord(
        id=question.id,
        language=question.language,
        outcome="answer",
        reason=None,
        expected_metrics=(question.expected_metric,),
        rankings=rankings,
        confidence=band,
    )


def band_summary(details: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """How many valid questions fall in each confidence band, and how often the right metric is in
    the first 3 of the fused search for those that are `low`. Every held-out question is valid
    (it has a defined metric), so a `low` band is always a false abstention."""

    def count(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        low = [d for d in rows if d["confidence"] == "low"]
        return {
            "n": len(rows),
            "bands": {b: sum(1 for d in rows if d["confidence"] == b) for b in BANDS},
            "low_found_at_3": sum(1 for d in low if d["found_at_3"]["fused"]),
            "found_at_3_by_band": {
                b: sum(1 for d in rows if d["confidence"] == b and d["found_at_3"]["fused"])
                for b in BANDS
            },
        }

    return {
        **count(details),
        "by_language": {
            language: count([d for d in details if d["language"] == language])
            for language in ("en", "id")
        },
    }


def run_heldout(engine: Engine, searchers: Searchers, heldout: HeldoutFile) -> dict[str, Any]:
    """Search every held-out question with the fused search and each leg alone; score; describe
    the confidence bands. Returns `retrieval.heldout` of the report."""
    records: list[RetrievalRecord] = []
    details: list[dict[str, Any]] = []
    for question in heldout.questions:
        results, rankings = rank_systems(engine, searchers, question.question)
        fused = results["fused"]
        record = _record(question, rankings, fused.confidence.band)
        records.append(record)
        details.append(
            {
                "id": question.id,
                "question": question.question,
                "language": question.language,
                "expected": [question.expected_metric],
                "confidence": fused.confidence.band,
                "coverage": round(fused.confidence.coverage, 4),
                "top_cosine": (
                    None
                    if fused.confidence.top_cosine is None
                    else round(fused.confidence.top_cosine, 4)
                ),
                "ranked_ids": fused.ids,
                "rankings": {name: list(rankings[name]) for name in SYSTEMS},
                "found_at_3": {
                    name: recall_hit(record, rankings[name], RECALL_K) for name in SYSTEMS
                },
                "reciprocal_rank": {
                    name: reciprocal_rank(record, rankings[name]) for name in SYSTEMS
                },
            }
        )
    return {
        "n_questions": len(records),
        "provenance": heldout.provenance,
        "kinds": list(KINDS),
        "limit": LIMIT,
        "role": ROLE,
        "scores": score_all(records),
        "confidence": band_summary(details),
        "questions": details,
    }
