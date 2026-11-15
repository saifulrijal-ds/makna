"""Hand-made pieces for the eval unit tests: a small report and fake compile and run functions."""

from __future__ import annotations

import copy
from typing import Any

from evals.compile_eval import Item, part_summary
from evals.scores import RetrievalRecord, latency_summary, score_all


def retrieval_section(
    *, recall_ok: bool = True, latency: list[float] | None = None
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
    return {
        "n_questions": 10,
        "kinds": ["metric", "glossary_term"],
        "limit": 10,
        "role": "viewer",
        "scores": score_all(records),
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
