"""The resolver score (docs/EVALS.md section 5.4, last paragraph; WP-20 acceptance 6).

The rule-based resolver (`makna.resolve`) reads each golden question with today = 2026-07-01 and
builds a request. The score is its exact-request accuracy on the `answer` questions: the request
it builds equals `expected.request`, compared as `QueryRequest` values (a filter, a dimension, a
grain, and both dates must all be right). It is the baseline the agent comparison is read against,
and it is informative only: `evals/gates.toml` has no resolver gate.

It goes through `makna.services.resolve_on_snapshot`, the one resolve path of the product (the
service calls it too), so the figures are those of the test bench. It searches for role `viewer`,
which is what the bench does for every role, and logs nothing. The held-out questions are never
used here.

Besides the headline figure the section reports the 20 first questions (G01 to G20, the set of
WP-20 acceptance 2), and, as information, how the `clarify` and the no-answer questions came out.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Engine

from evals.golden.schema import Question
from evals.runtime import TODAY
from makna.model.governance import GovernancePolicy
from makna.model.request import QueryRequest
from makna.resolve import Resolution
from makna.search.hybrid import HybridSearch
from makna.services import resolve_on_snapshot
from makna.services.snapshot import Snapshot

FIRST_QUESTIONS = 20
"""WP-20 acceptance 2 is about G01 to G20."""


def _number(question: Question) -> int:
    return int(question.id[1:])


def _request_dict(request: QueryRequest | None) -> dict[str, Any] | None:
    return None if request is None else request.model_dump(mode="json", exclude_defaults=True)


def _answer_item(question: Question, resolution: Resolution) -> dict[str, Any]:
    assert question.expected.request is not None
    expected = question.expected.request.to_query_request()
    return {
        "exact": resolution.request == expected,
        "expected_request": _request_dict(expected),
        "request": _request_dict(resolution.request),
        "metric_found": resolution.request is not None
        and resolution.request.metric == expected.metric,
    }


def _clarify_item(question: Question, resolution: Resolution) -> dict[str, Any]:
    assert question.expected.candidates is not None
    wanted = sorted(c.metric for c in question.expected.candidates)
    pair = sorted(i.partition(":")[2] for i in resolution.ambiguous_between)
    return {
        "exact": resolution.status == "ambiguous" and set(wanted) <= set(pair),
        "expected_candidates": wanted,
        "ambiguous_between": pair,
    }


def _no_answer_item(question: Question, resolution: Resolution) -> dict[str, Any]:
    # Nothing is expected to resolve: a `no_match` (or an ambiguity, which also builds no request)
    # abstains; a resolved request is what a careless resolver does.
    return {"exact": resolution.request is None, "request": _request_dict(resolution.request)}


def run_resolver(
    search: HybridSearch,
    engine: Engine,
    snapshot: Snapshot,
    policy: GovernancePolicy,
    questions: Sequence[Question],
    *,
    today: str = TODAY,
) -> dict[str, Any]:
    """Resolve every golden question and score the `answer`, `clarify`, and no-answer ones."""
    day = datetime.date.fromisoformat(today)
    items: list[dict[str, Any]] = []
    for question in questions:
        resolution = resolve_on_snapshot(search, engine, snapshot, question.question, day, policy)
        if question.outcome == "answer":
            detail = _answer_item(question, resolution)
        elif question.outcome == "clarify":
            detail = _clarify_item(question, resolution)
        else:
            detail = _no_answer_item(question, resolution)
        items.append(
            {
                "id": question.id,
                "outcome": question.outcome,
                "language": question.language,
                "status": resolution.status,
                "confidence": resolution.confidence,
                "unresolved_terms": list(resolution.unresolved_terms),
                "first_candidates": [c.id for c in resolution.candidates[:3]],
                **detail,
            }
        )
    return {
        "today": today,
        "n_questions": len(items),
        "answer": _score([i for i in items if i["outcome"] == "answer"]),
        "answer_first_20": _score(
            [
                i
                for i, q in zip(items, questions, strict=True)
                if i["outcome"] == "answer" and _number(q) <= FIRST_QUESTIONS
            ]
        ),
        "clarify": _score([i for i in items if i["outcome"] == "clarify"]),
        "no_answer": _score([i for i in items if i["outcome"] in ("no_metric", "refuse")]),
        "by_language": {
            language: _score(
                [i for i in items if i["outcome"] == "answer" and i["language"] == language]
            )
            for language in ("en", "id", "mixed")
        },
        "items": items,
    }


def _score(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    total = len(items)
    exact = sum(1 for i in items if i["exact"])
    statuses = {
        status: sum(1 for i in items if i["status"] == status)
        for status in ("resolved", "ambiguous", "no_match")
    }
    return {
        "total": total,
        "exact": exact,
        "rate": (exact / total) if total else None,
        "by_status": statuses,
        "ids_exact": [i["id"] for i in items if i["exact"]],
        "ids_not_exact": [i["id"] for i in items if not i["exact"]],
    }
