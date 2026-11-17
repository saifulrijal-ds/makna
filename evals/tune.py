"""Tuning the search weights on the tuned split only (docs/EVALS.md section 7, WP-14C).

**This module reads the tuned split and nothing else**: the golden questions
(`evals/golden/questions.yaml`) and the search questions (`evals/golden/search_questions.yaml`).
It cannot reach the held-out questions: it imports nothing that knows their file, and
`tests/unit/test_eval_heldout.py` walks the imports of this module and fails if that changes.
Keep it so. A weight chosen by looking at the held-out numbers is tuned on them, and the split
then measures nothing.

The selection rule, fixed before the held-out numbers of this work were looked at:

1. **Candidates.** A setting is a candidate only if the tuned-split gates still pass under it
   (recall at 3, mean reciprocal rank, false abstention rate, English-minus-Indonesian gap; the
   thresholds of `evals/gates.toml`) and both legs keep a positive weight (one leg alone is not a
   hybrid search; the single-leg figures are reported, not chosen).
2. **Score.** The pooled mean reciprocal rank over the tuned split: the golden `answer` questions
   (the gated search: kinds `metric` and `glossary_term`) and the search questions (the search of
   an agent with no `kinds`), one reciprocal rank each, rank 10 as the depth.
3. **Ties.** Settings within `TIE` (0.005) of the best score tie. Of the tied, take the one
   nearest to equal weights (smallest absolute logarithm of `w_vector / w_keyword`); for the
   glossary term weight, the one nearest to the current weight; for the confidence threshold the
   highest.
4. **Order.** First the ratio `w_vector / w_keyword` (with `w_keyword` fixed at 1, because only
   the ratio matters in rank fusion), then the glossary term kind weight at that ratio, then the
   `low` confidence threshold at both: the smallest false abstention rate over the golden
   `answer` questions, with the tuned-split gates still passing.

Nothing is written to `config/makna.toml`: the command prints the setting it chose and the
person who commits it copies it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from evals import gates as gates_module
from evals import retrieval
from evals.golden.reference import QUESTIONS_FILE, SEARCH_QUESTIONS_FILE
from evals.golden.schema import (
    Question,
    SearchQuestion,
    load_questions,
    load_search_questions,
)
from evals.scores import (
    MRR_DEPTH,
    RECALL_K,
    RetrievalRecord,
    distinct_metrics,
    score_all,
)
from makna.model.search import ConfidenceSettings, EmbeddingProvider, SearchSettings
from makna.search.hybrid import HybridSearch

TIE = 0.005
RATIOS = (1 / 16, 1 / 8, 1 / 4, 1 / 2, 3 / 4, 1.0, 4 / 3, 2.0, 4.0, 8.0, 16.0)
"""`w_vector / w_keyword` of the first stage; `1/16` is the setting of WP-15 (4.0 and 0.25)."""
SINGLE_LEG = (0.0, math.inf)
"""Reported for reference (keyword only, vector only), never chosen."""
GLOSSARY_WEIGHTS = (0.90, 0.92, 0.94, 0.96, 0.98, 1.0)
LOW_THRESHOLDS = (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50)


def load_tuned_questions() -> tuple[list[Question], list[SearchQuestion]]:
    """The only questions the tuning reads."""
    return load_questions(QUESTIONS_FILE), load_search_questions(SEARCH_QUESTIONS_FILE)


class QueryCache:
    """Embeds each query once, so that a grid of settings costs one embedding per question."""

    def __init__(self, provider: EmbeddingProvider) -> None:
        self._provider = provider
        self._vectors: dict[str, list[float]] = {}
        self.model_id = provider.model_id

    @property
    def dimension(self) -> int:
        return self._provider.dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._provider.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        if text not in self._vectors:
            self._vectors[text] = self._provider.embed_query(text)
        return self._vectors[text]


@dataclass(frozen=True)
class Point:
    """One setting and what it scores on the tuned split."""

    w_keyword: float
    w_vector: float
    glossary_weight: float
    low: float
    scores: dict[str, float | None]
    gates_pass: bool
    pooled_mrr: float

    def ratio(self) -> float:
        return self.w_vector / self.w_keyword if self.w_keyword else math.inf


@dataclass
class Tuner:
    engine: Engine
    provider: EmbeddingProvider
    base: SearchSettings
    questions: Sequence[Question]
    search_questions: Sequence[SearchQuestion]
    thresholds: dict[str, float]
    _related: dict[str, list[str]] = field(default_factory=dict)

    def settings(
        self, ratio: float, glossary: float | None = None, low: float | None = None
    ) -> SearchSettings:
        """The base settings with `w_keyword` 1 and `w_vector` the ratio (a ratio of 0 or
        infinity is one leg alone), and optionally another glossary term weight or `low`."""
        if math.isinf(ratio):
            w_keyword, w_vector = 0.0, 1.0
        else:
            w_keyword, w_vector = 1.0, ratio
        update: dict[str, Any] = {"w_keyword": w_keyword, "w_vector": w_vector}
        if glossary is not None:
            update["kind_weights"] = {**self.base.kind_weights, "glossary_term": glossary}
        if low is not None:
            update["confidence"] = ConfidenceSettings(
                high=self.base.confidence.high,
                low=low,
                low_cosine=self.base.confidence.low_cosine,
            )
        return self.base.model_copy(update=update)

    def _metrics_of(self, ids: list[str]) -> list[str]:
        missing = [i for i in ids if i.startswith("glossary_term:") and i not in self._related]
        if missing:
            found = retrieval.related_metrics(self.engine, missing)
            for term in missing:
                self._related[term] = found.get(term, [])
        return distinct_metrics(ids, self._related)

    def evaluate(self, settings: SearchSettings) -> Point:
        searcher = HybridSearch(self.engine, self.provider, settings)
        records: list[RetrievalRecord] = []
        for question in self.questions:
            result = retrieval.search_hits(searcher, question.question)
            ranked = self._metrics_of(result.ids)
            records.append(
                retrieval.question_record(question, {"fused": ranked}, result.confidence.band)
            )
        scores = score_all(records)["fused"]
        by_language = scores["by_language"]
        english, indonesian = by_language["en"]["recall_at_3"], by_language["id"]["recall_at_3"]
        gap = None if english is None or indonesian is None else english - indonesian
        reciprocal = [
            _reciprocal(ranked_rr)
            for ranked_rr in (
                (r.expected_metrics, r.rankings["fused"]) for r in records if r.outcome == "answer"
            )
        ]
        found_3: list[float] = []
        for item in self.search_questions:
            ids = retrieval.default_search_hits(searcher, item.question).ids
            rank = next((i for i, x in enumerate(ids[:MRR_DEPTH], 1) if x in item.expected), None)
            reciprocal.append(0.0 if rank is None else 1.0 / rank)
            found_3.append(float(rank is not None and rank <= RECALL_K))
        flat = {
            "recall_at_3": scores["recall_at_3"],
            "recall_at_1": scores["recall_at_1"],
            "mrr": scores["mrr"],
            "false_abstention_rate": scores["false_abstention_rate"],
            "abstention_recall": scores["abstention_recall"],
            "indonesian_gap": gap,
            "search_questions_recall_at_3": sum(found_3) / len(found_3) if found_3 else None,
        }
        thresholds = self.thresholds
        passes = (
            _at_least(flat["recall_at_3"], thresholds["recall_at_3_min"])
            and _at_least(flat["mrr"], thresholds["mrr_min"])
            and _at_most(flat["false_abstention_rate"], thresholds["false_abstention_rate_max"])
            and _at_most(gap, thresholds["indonesian_recall_gap_max"])
        )
        return Point(
            settings.w_keyword,
            settings.w_vector,
            settings.kind_weights["glossary_term"],
            settings.confidence.low,
            flat,
            passes,
            sum(reciprocal) / len(reciprocal) if reciprocal else 0.0,
        )


def _reciprocal(pair: tuple[Sequence[str], Sequence[str]]) -> float:
    expected, ranked = pair
    for rank, name in enumerate(ranked[:MRR_DEPTH], start=1):
        if name in expected:
            return 1.0 / rank
    return 0.0


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold - gates_module.EPSILON


def _at_most(value: float | None, threshold: float) -> bool:
    return value is not None and value <= threshold + gates_module.EPSILON


def choose(
    points: Sequence[Point],
    *,
    key: Callable[[Point], float],
    nearest: Callable[[Point], float],
) -> Point | None:
    """Rule items 1 and 3: among the points whose gates pass, those within `TIE` of the best
    `key` tie, and the one with the smallest `nearest` wins. None when no point passes."""
    candidates = [p for p in points if p.gates_pass]
    if not candidates:
        return None
    best = max(key(p) for p in candidates)
    tied = [p for p in candidates if key(p) >= best - TIE]
    return min(tied, key=nearest)


def tune(tuner: Tuner, *, log: Callable[[str], None] = lambda message: None) -> dict[str, Any]:
    """The three stages of the rule. Returns every point and the choice of each stage."""
    stage1 = []
    for ratio in (*SINGLE_LEG, *RATIOS):
        log(f"ratio {ratio:g}")
        stage1.append((ratio, tuner.evaluate(tuner.settings(ratio))))
    hybrid = [point for ratio, point in stage1 if ratio not in SINGLE_LEG]
    chosen1 = choose(hybrid, key=lambda p: p.pooled_mrr, nearest=lambda p: abs(math.log(p.ratio())))
    if chosen1 is None:
        raise RuntimeError("no setting passes the tuned-split gates; nothing to choose")
    ratio = chosen1.ratio()

    current_glossary = tuner.base.kind_weights["glossary_term"]
    stage2 = [(g, tuner.evaluate(tuner.settings(ratio, glossary=g))) for g in GLOSSARY_WEIGHTS]
    chosen2 = choose(
        [p for _, p in stage2],
        key=lambda p: p.pooled_mrr,
        nearest=lambda p: abs(p.glossary_weight - current_glossary),
    )
    assert chosen2 is not None
    glossary = chosen2.glossary_weight

    stage3 = [
        (low, tuner.evaluate(tuner.settings(ratio, glossary=glossary, low=low)))
        for low in LOW_THRESHOLDS
    ]
    chosen3 = choose(
        [p for _, p in stage3],
        key=lambda p: -(p.scores["false_abstention_rate"] or 0.0),
        nearest=lambda p: -p.low,
    )
    assert chosen3 is not None
    return {
        "rule": "evals/tune.py module docstring",
        "tie": TIE,
        "base": {
            "w_keyword": tuner.base.w_keyword,
            "w_vector": tuner.base.w_vector,
            "glossary_weight": current_glossary,
            "low": tuner.base.confidence.low,
        },
        "ratio_grid": [_point_json(p, ratio=r) for r, p in stage1],
        "glossary_grid": [_point_json(p) for _, p in stage2],
        "low_grid": [_point_json(p) for _, p in stage3],
        "chosen": {
            "w_keyword": 1.0,
            "w_vector": ratio,
            "glossary_term_weight": glossary,
            "low": chosen3.low,
            "pooled_mrr": chosen3.pooled_mrr,
            "scores": chosen3.scores,
        },
    }


def _point_json(point: Point, *, ratio: float | None = None) -> dict[str, Any]:
    ratio = point.ratio() if ratio is None else ratio
    return {
        "ratio": "inf" if math.isinf(ratio) else ratio,
        "w_keyword": point.w_keyword,
        "w_vector": point.w_vector,
        "glossary_weight": point.glossary_weight,
        "low": point.low,
        "gates_pass": point.gates_pass,
        "pooled_mrr": point.pooled_mrr,
        **point.scores,
    }


def render_markdown(result: dict[str, Any]) -> str:
    def row(entry: dict[str, Any], first: str) -> str:
        def num(value: Any) -> str:
            return "n/a" if value is None else f"{value:.3f}"

        return (
            f"| {first} | {num(entry['pooled_mrr'])} | {num(entry['recall_at_3'])} | "
            f"{num(entry['mrr'])} | {num(entry['recall_at_1'])} | "
            f"{num(entry['search_questions_recall_at_3'])} | {num(entry['false_abstention_rate'])}"
            f" | {num(entry['abstention_recall'])} | {num(entry['indonesian_gap'])} | "
            f"{'yes' if entry['gates_pass'] else 'NO'} |"
        )

    header = (
        "| Setting | Pooled MRR | Recall at 3 | MRR | Recall at 1 | Search questions at 3 | "
        "False abstention | Abstention recall | en minus id | Gates |\n"
        "|---|---|---|---|---|---|---|---|---|---|"
    )
    chosen = result["chosen"]
    lines = [
        "# Tuning on the tuned split",
        "",
        "Only the golden questions and the search questions were read (`evals/tune.py`). The rule "
        "is in the module docstring.",
        "",
        "## Weight ratio (w_keyword 1, w_vector = ratio)",
        "",
        header,
    ]
    lines += [row(e, f"ratio {e['ratio']}") for e in result["ratio_grid"]]
    lines += ["", "## Glossary term kind weight at the chosen ratio", "", header]
    lines += [row(e, f"glossary {e['glossary_weight']}") for e in result["glossary_grid"]]
    lines += ["", "## `low` confidence threshold at both", "", header]
    lines += [row(e, f"low {e['low']}") for e in result["low_grid"]]
    lines += [
        "",
        "## Chosen",
        "",
        f"`w_keyword = {chosen['w_keyword']}`, `w_vector = {chosen['w_vector']}`, "
        f"`kind_weights.glossary_term = {chosen['glossary_term_weight']}`, "
        f"`confidence.low = {chosen['low']}`; pooled MRR {chosen['pooled_mrr']:.3f}.",
        "",
    ]
    return "\n".join(lines)


def write_result(result: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "tuning.json", out_dir / "tuning.md"
    json_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path
