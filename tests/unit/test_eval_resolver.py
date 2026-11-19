"""WP-20 acceptance 6: the resolver score of the evals (docs/EVALS.md section 6), on fabricated
search results, so no database and no model are needed."""

from __future__ import annotations

import datetime
from typing import Any, cast

from evals import gates, report, resolver_eval
from evals.golden.schema import Question
from evals.runtime import TODAY
from makna.model.governance import STRICT
from makna.search.confidence import Band, Confidence
from makna.search.hybrid import HybridSearch, SearchResult
from makna.services.snapshot import Snapshot, StateKey
from tests.unit.compiler_support import lending
from tests.unit.test_eval_report import SETTINGS
from tests.unit.test_resolver import hit

from .eval_support import full_report


def question(number: int, text: str, outcome: str, expected: dict[str, Any]) -> Question:
    return Question.model_validate(
        {
            "id": f"G{number:02d}",
            "question": text,
            "language": "en",
            "group": {
                "answer": "direct_english",
                "clarify": "clarify",
                "no_metric": "no_metric",
            }[outcome],
            "tags": [],
            "outcome": outcome,
            "expected": expected,
        }
    )


class FakeSearch:
    """Answers each question with the hits fabricated for it."""

    def __init__(self, canned: dict[str, tuple[Band, list[Any]]]) -> None:
        self.canned = canned
        self.calls: list[dict[str, Any]] = []

    def search(self, text: str, **kwargs: Any) -> SearchResult:
        self.calls.append({"text": text, **kwargs})
        band, hits = self.canned[text]
        return SearchResult(text, tuple(hits), Confidence(band, 1.0, (), (), None))


QUESTIONS = [
    question(
        1,
        "How much did we disburse last month by region",
        "answer",
        {
            "metrics": ["disbursement_amount"],
            "request": {
                "metric": "disbursement_amount",
                "dimensions": ["branch.region_name"],
                "time_start": "2026-06-01",
                "time_end": "2026-07-01",
            },
            "answer_columns": ["x"],
        },
    ),
    question(
        2,
        "What is the NPF ratio in 2025",
        "answer",
        {
            "metrics": ["npf_ratio"],
            "request": {"metric": "npf_ratio"},  # wrong on purpose: the resolver adds the year
            "answer_columns": ["x"],
        },
    ),
    question(
        3,
        "Berapa NPF kita",
        "clarify",
        {
            "candidates": [
                {
                    "metric": "npf_ratio",
                    "request": {"metric": "npf_ratio"},
                    "answer_columns": ["a"],
                },
                {
                    "metric": "npf_outstanding_principal",
                    "request": {"metric": "npf_outstanding_principal"},
                    "answer_columns": ["b"],
                },
            ]
        },
    ),
    question(4, "net interest margin", "no_metric", {"reason": "no_definition"}),
]
CANNED: dict[str, tuple[Band, list[Any]]] = {
    QUESTIONS[0].question: (
        "high",
        [
            hit("metric:disbursement_amount", 0.0184, 1.0, ("disburse",)),
            hit("metric:due_amount", 0.017, 0.1),
        ],
    ),
    QUESTIONS[1].question: (
        "high",
        [
            hit("metric:npf_ratio", 0.0184, 1.0, ("npf", "ratio")),
            hit("metric:due_amount", 0.017, 0.1),
        ],
    ),
    QUESTIONS[2].question: (
        "high",
        [
            hit("metric:npf_ratio", 0.01841, 1.0, ("npf",)),
            hit("metric:npf_outstanding_principal", 0.01818, 1.0, ("npf",)),
        ],
    ),
    QUESTIONS[3].question: ("low", [hit("metric:due_amount", 0.017, 0.0)]),
}


def run() -> tuple[dict[str, Any], FakeSearch]:
    search = FakeSearch(CANNED)
    snapshot = Snapshot(StateKey("v", "m", "i"), lending())
    section = resolver_eval.run_resolver(
        cast(HybridSearch, search), cast(Any, None), snapshot, STRICT, QUESTIONS
    )
    return section, search


def test_the_score_counts_exact_requests_on_the_answer_questions() -> None:
    section, _ = run()
    assert section["today"] == "2026-07-01"
    assert section["answer"]["total"] == 2 and section["answer"]["exact"] == 1
    assert section["answer"]["rate"] == 0.5
    assert section["answer"]["ids_exact"] == ["G01"] and section["answer"]["ids_not_exact"] == [
        "G02"
    ]
    assert section["answer_first_20"]["total"] == 2
    first, second = section["items"][:2]
    assert first["exact"] is True and first["status"] == "resolved"
    assert second["exact"] is False and second["request"]["time_start"] == "2025-01-01"
    assert second["expected_request"] == {"metric": "npf_ratio"}


def test_clarify_and_no_answer_questions_are_scored_for_information() -> None:
    section, _ = run()
    assert (section["clarify"]["exact"], section["clarify"]["total"]) == (1, 1)
    assert section["items"][2]["ambiguous_between"] == [
        "npf_outstanding_principal",
        "npf_ratio",
    ]
    assert (section["no_answer"]["exact"], section["no_answer"]["total"]) == (1, 1)
    assert section["items"][3]["status"] == "no_match"


def test_the_search_is_the_one_the_bench_makes_and_today_is_the_golden_date() -> None:
    _, search = run()
    assert {c["role"] for c in search.calls} == {"viewer"}
    assert {tuple(c["kinds"]) for c in search.calls} == {("metric", "glossary_term")}
    # Nothing but the date in the file header counts: not the clock.
    again, _ = run()
    assert again == run()[0]
    assert TODAY == "2026-07-01"
    assert datetime.date.fromisoformat(section_today()) == datetime.date(2026, 7, 1)


def section_today() -> str:
    return str(run()[0]["today"])


def test_the_report_carries_the_resolver_section_and_no_gate_for_it() -> None:
    section, _ = run()
    parts = full_report()
    results = gates.evaluate(parts, gates.load_gates())
    document = report.build_report(
        "all",
        SETTINGS,
        results,
        retrieval=parts["retrieval"],
        compile_section=parts["compile"],
        resolver=section,
    )
    assert document["resolver"]["answer"]["exact"] == 1
    assert not [g for g in document["gates"] if "resolver" in g["name"]]
    markdown = report.render_markdown(document)
    assert "## Resolver (rule-based, no gate)" in markdown
    assert "1 of 2" in markdown and "G01 to G20" in markdown
    assert "| G02 |" in markdown  # the question that is not exact is listed
    # Without the section nothing about the resolver is printed.
    plain = report.build_report("retrieval", SETTINGS, [], retrieval=parts["retrieval"])
    assert "resolver" not in plain and "Resolver" not in report.render_markdown(plain)


def test_the_gates_file_has_no_resolver_gate() -> None:
    assert "resolver" not in gates.load_gates()
