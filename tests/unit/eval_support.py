"""Hand-made pieces for the eval unit tests: a small report and fake compile and run functions."""

from __future__ import annotations

import copy
from typing import Any

from evals.compile_eval import Item, part_summary
from evals.scores import RetrievalRecord, latency_summary, score_all


def retrieval_section(
    *, recall_ok: bool = True, latency: list[float] | None = None, heldout_recall: float = 0.8
) -> dict[str, Any]:
    """A `retrieval` section with 10 `answer` questions: English and Indonesian, all found
    (or, with `recall_ok=False`, the Indonesian ones missed)."""
    records = []
    for i in range(10):
        language = "en" if i < 5 else "id"
        found = recall_ok or language == "en"
        ranked = ["m"] if found else ["x", "y", "z", "w", "m"]
        records.append(
            RetrievalRecord(
                f"G{i + 1:02d}",
                language,
                "answer",
                None,
                expected_metrics=("m",),
                rankings={"fused": ranked, "keyword": ranked, "vector": ranked},
                confidence="high",
            )
        )
    held = []
    for i in range(10):
        found = i < round(heldout_recall * 10)
        ranked = ["m"] if found else ["x", "y", "z", "w", "m"]
        held.append(
            RetrievalRecord(
                f"H{i + 1:02d}",
                "en" if i % 2 else "id",
                "answer",
                None,
                expected_metrics=("m",),
                rankings={"fused": ranked, "keyword": ranked, "vector": ranked},
                confidence="medium",
            )
        )
    return {
        "heldout": {
            "n_questions": 10,
            "provenance": "Written by a fresh agent that never read the repository (D-34).",
            "scores": score_all(held),
            "confidence": {
                "n": 10,
                "bands": {"high": 0, "medium": 10, "low": 0},
                "low_found_at_3": 0,
                "found_at_3_by_band": {"high": 0, "medium": 8, "low": 0},
                "by_language": {
                    language: {
                        "n": 5,
                        "bands": {"high": 0, "medium": 5, "low": 0},
                        "low_found_at_3": 0,
                        "found_at_3_by_band": {"high": 0, "medium": 4, "low": 0},
                    }
                    for language in ("en", "id")
                },
            },
            "questions": [],
        },
        "n_questions": 10,
        "kinds": ["metric", "glossary_term"],
        "limit": 10,
        "role": "viewer",
        "scores": score_all(records),
        "by_kind": {
            "kinds": ["metric", "entity", "dimension", "rule", "glossary_term", "domain"],
            "limit": 10,
            "rows": [
                {"kind": "metric", "source": "golden", "n_questions": 10, "recall_at_3": 0.9},
                {"kind": "glossary_term", "source": "s", "n_questions": 2, "recall_at_3": 1.0},
                {"kind": "dimension", "source": "s", "n_questions": 4, "recall_at_3": 0.75},
                {"kind": "entity", "source": "s", "n_questions": 0, "recall_at_3": None},
            ],
            "questions": [
                {
                    "id": "S01",
                    "question": "q",
                    "language": "en",
                    "kind": "dimension",
                    "expected": ["dimension:a.b"],
                    "found_at_3": False,
                    "rank": None,
                    "ranked_ids": ["metric:x", "metric:y", "metric:z"],
                }
            ],
        },
        "latency": {
            **latency_summary(latency or [10.0, 20.0, 30.0]),
            "rounds": 1,
            "warmup_ms": 1.0,
        },
        "questions": [],
    }


def compile_section(*, failing: dict[str, tuple[str, str]] | None = None) -> dict[str, Any]:
    """A `compile` section; `failing` maps a part name to the `(id, reason)` of one failure."""
    failing = failing or {}
    section: dict[str, Any] = {}
    for part in ("part_a", "part_b", "part_c"):
        items = [Item(f"{part}-{i}", None, rows=1) for i in range(4)]
        if part in failing:
            items[0] = Item(failing[part][0], failing[part][1])
        section[part] = part_summary(items)
    return section


def full_report(**kwargs: Any) -> dict[str, Any]:
    return {
        "retrieval": retrieval_section(**kwargs.get("retrieval", {})),
        "compile": compile_section(**kwargs.get("compile", {})),
    }


def lowered(
    gates: dict[str, dict[str, float]], section: str, key: str, value: float
) -> dict[str, dict[str, float]]:
    changed = copy.deepcopy(gates)
    changed[section][key] = value
    return changed
