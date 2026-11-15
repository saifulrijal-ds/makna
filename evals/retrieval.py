"""The retrieval eval (EVL-2, docs/EVALS.md section 3).

It calls the same search code the `search` service calls: `HybridSearch` with the configured
search settings and embedding provider, as a viewer would (approved objects only), with kinds
`metric` and `glossary_term` and limit 10. It does not go through `Services.search`, which writes
a `tool_call_log` row per call; an integration test keeps the two from drifting.

Keyword-only and vector-only scores run the same search with the other leg's weight set to 0. The
fusion statement keeps only documents whose fused score is above zero, so a zero weight leaves
exactly the other leg's candidates; their order is that leg's rank times the status and kind
weights (a glossary term sits a few places lower than a metric of the same leg rank, as in the
fused search).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from evals.golden.schema import Question
from evals.scores import (
    RECALL_K,
    SYSTEMS,
    RetrievalRecord,
    distinct_metrics,
    latency_summary,
    recall_hit,
    reciprocal_rank,
    score_all,
)
from makna.model.search import EmbeddingProvider, SearchSettings
from makna.search.hybrid import HybridSearch, SearchResult

KINDS = ["metric", "glossary_term"]
LIMIT = 10
ROLE = "viewer"
"""The search role of the service layer for a viewer or an agent token (EVALS section 1 says
agent token): approved objects only and no personal columns, the same for both; the drift test
checks both callers."""
LATENCY_ROUNDS = 3
"""Every question is searched this many times to time it; the first round is the one scored."""


@dataclass(frozen=True)
class Searchers:
    fused: HybridSearch
    keyword: HybridSearch
    vector: HybridSearch

    def by_system(self) -> dict[str, HybridSearch]:
        return {"fused": self.fused, "keyword": self.keyword, "vector": self.vector}


def make_searchers(
    engine: Engine, provider: EmbeddingProvider, settings: SearchSettings
) -> Searchers:
    """The configured search, and the same search with one leg's weight set to 0."""
    return Searchers(
        fused=HybridSearch(engine, provider, settings),
        keyword=HybridSearch(engine, provider, settings.model_copy(update={"w_vector": 0.0})),
        vector=HybridSearch(engine, provider, settings.model_copy(update={"w_keyword": 0.0})),
    )


def search_hits(searcher: HybridSearch, query: str) -> SearchResult:
    """One search as the eval makes it. The query is stripped, as the service strips it."""
    return searcher.search(query.strip(), role=ROLE, kinds=list(KINDS), limit=LIMIT)


def related_metrics(engine: Engine, object_ids: Sequence[str]) -> dict[str, list[str]]:
    """`glossary_term:` id -> the `metric:` ids of its `related` list, from the stored official
    definition (a term that is not approved has none)."""
    terms = sorted({i for i in object_ids if i.startswith("glossary_term:")})
    if not terms:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, official FROM semantic_object "
                "WHERE id = ANY(:ids) AND official IS NOT NULL"
            ),
            {"ids": terms},
        )
        return {
            row.id: [r for r in (row.official.get("related") or []) if r.startswith("metric:")]
            for row in rows
        }


def _record(question: Question, rankings: dict[str, list[str]], confidence: str) -> RetrievalRecord:
    expected = question.expected
    return RetrievalRecord(
        id=question.id,
        language=question.language,
        outcome=question.outcome,
        reason=expected.reason,
        expected_metrics=tuple(expected.metrics or ()) if question.outcome == "answer" else (),
        candidates=(
            tuple(c.metric for c in expected.candidates or ())
            if question.outcome == "clarify"
            else ()
        ),
        rankings=rankings,
        confidence=confidence,
    )


def run_retrieval(
    engine: Engine,
    searchers: Searchers,
    questions: Sequence[Question],
    *,
    rounds: int = LATENCY_ROUNDS,
) -> dict[str, Any]:
    """Search every question with the fused search and each leg alone; score; time the fused
    search. Returns the `retrieval` section of the report."""
    # The first call loads the embedding model. It is timed apart so that the latency figure
    # describes a search on a running server, and reported so that nothing is hidden.
    started = time.perf_counter()
    search_hits(searchers.fused, questions[0].question)
    warmup_ms = (time.perf_counter() - started) * 1000

    records: list[RetrievalRecord] = []
    details: list[dict[str, Any]] = []
    times: list[float] = []
    for round_index in range(max(1, rounds)):
        for question in questions:
            started = time.perf_counter()
            fused = search_hits(searchers.fused, question.question)
            elapsed = (time.perf_counter() - started) * 1000
            times.append(elapsed)
            if round_index > 0:
                continue
            legs = {
                "keyword": search_hits(searchers.keyword, question.question),
                "vector": search_hits(searchers.vector, question.question),
            }
            results: Mapping[str, SearchResult] = {"fused": fused, **legs}
            all_ids = [i for r in results.values() for i in r.ids]
            related = related_metrics(engine, all_ids)
            rankings = {name: distinct_metrics(r.ids, related) for name, r in results.items()}
            record = _record(question, rankings, fused.confidence.band)
            records.append(record)
            details.append(_detail(question, record, results, elapsed))
    return {
        "n_questions": len(questions),
        "kinds": KINDS,
        "limit": LIMIT,
        "role": ROLE,
        "scores": score_all(records),
        "latency": {
            **latency_summary(times),
            "rounds": max(1, rounds),
            "warmup_ms": warmup_ms,
        },
        "questions": details,
    }


def _detail(
    question: Question,
    record: RetrievalRecord,
    results: Mapping[str, SearchResult],
    elapsed_ms: float,
) -> dict[str, Any]:
    fused = results["fused"]
    return {
        "id": question.id,
        "question": question.question,
        "language": question.language,
        "group": question.group,
        "outcome": question.outcome,
        "reason": question.expected.reason,
        "expected": list(record.expected_metrics or record.candidates),
        "confidence": fused.confidence.band,
        "coverage": round(fused.confidence.coverage, 4),
        "top_cosine": (
            None if fused.confidence.top_cosine is None else round(fused.confidence.top_cosine, 4)
        ),
        "ranked_ids": fused.ids,
        "rankings": {name: list(record.rankings[name]) for name in SYSTEMS},
        "found_at_3": {
            name: recall_hit(record, record.rankings[name], RECALL_K) for name in SYSTEMS
        },
        "reciprocal_rank": {
            name: reciprocal_rank(record, record.rankings[name]) for name in SYSTEMS
        },
        "time_ms": round(elapsed_ms, 2),
    }
