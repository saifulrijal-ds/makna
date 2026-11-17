"""WP-15: the report holds every score of docs/EVALS.md sections 3 and 4 and renders from JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals import gates, report
from evals.scores import LANGUAGES, SYSTEMS

from .eval_support import full_report

SETTINGS: dict[str, Any] = {
    "search": {
        "w_keyword": 4.0,
        "w_vector": 0.25,
        "candidates": 30,
        "confidence": {"high": 0.6, "low": 0.3, "low_cosine": None},
        "status_weights": {},
        "kind_weights": {},
    },
    "embedding": {"provider": "hash", "model": "hash-trigram-256", "dimension": 256},
    "governance": {"mode": "strict", "min_group_size": 10, "fingerprint": "abc"},
    "dialect": "postgres",
    "today": "2026-07-01",
    "index": {"embedding_model": "hash-trigram-256", "definitions_version": "v", "documents": 3},
    "git": {"commit": "deadbeef", "dirty": False},
}

# Every score of the two tables (EVALS sections 3 and 4), by the name the report gives it.
RETRIEVAL_SCORES = ("recall_at_3", "recall_at_1", "mrr")
CONFIDENCE_SCORES = ("abstention_recall", "false_abstention_rate")


def build(failing: bool = False) -> dict[str, Any]:
    parts = full_report(
        compile={"failing": {"part_b": ("npf_ratio by x", "no_rows")}} if failing else {}
    )
    results = gates.evaluate(parts, gates.load_gates())
    return report.build_report(
        "all",
        SETTINGS,
        results,
        retrieval=parts["retrieval"],
        compile_section=parts["compile"],
    )


def test_the_report_holds_every_score() -> None:
    document = build()
    scores = document["retrieval"]["scores"]
    for system in SYSTEMS:
        for name in RETRIEVAL_SCORES:
            assert name in scores[system]
            for language in LANGUAGES:
                assert name in scores[system]["by_language"][language]
    for name in CONFIDENCE_SCORES:
        assert name in scores["fused"]
        assert name in scores["fused"]["by_language"]["id"]
    assert {"p50_ms", "p95_ms", "max_ms", "warmup_ms"} <= set(document["retrieval"]["latency"])
    for part in ("part_a", "part_b", "part_c"):
        assert {"total", "passed", "pass_rate", "failures"} <= set(document["compile"][part])
    assert document["settings"]["search"]["w_vector"] == 0.25
    assert document["passed"] is True


def test_the_markdown_is_rendered_from_the_json_and_names_every_score() -> None:
    document = json.loads(json.dumps(build()))
    text = report.render_markdown(document)
    for heading in (
        "## Gates",
        "## Retrieval (EVL-2)",
        "## Compile correctness (EVL-3)",
        "## Settings used",
    ):
        assert heading in text
    for label in (
        "Recall at 3",
        "Recall at 1",
        "MRR",
        "Abstention recall",
        "False abstention rate",
        "Latency",
    ):
        assert label in text
    for system in SYSTEMS:
        assert f"| {system} |" in text
    for language in LANGUAGES:
        assert f"| {language} | fused |" in text
    for part in ("A:", "B:", "C:"):
        assert part in text
    assert "deadbeef" in text and "4.0, 0.25" in text and "hash-trigram-256" in text
    assert text.startswith("# Makna eval report: PASS")


def test_the_report_shows_recall_at_3_by_kind_without_gating_it() -> None:
    document = json.loads(json.dumps(build()))
    rows = {r["kind"]: r for r in document["retrieval"]["by_kind"]["rows"]}
    assert set(rows) == {"metric", "glossary_term", "dimension", "entity"}
    text = report.render_markdown(document)
    assert "### Recall at 3 by kind of object" in text
    for kind in rows:
        assert f"| {kind} |" in text
    assert "| dimension | 0.750 | 4 |" in text and "| entity | n/a | 0 |" in text
    assert "S01 | dimension | dimension:a.b | none in 10 |" in text
    assert not any("kind" in gate["label"].lower() for gate in document["gates"])


def test_a_failing_report_says_fail_and_lists_the_failing_request() -> None:
    text = report.render_markdown(build(failing=True))
    assert text.startswith("# Makna eval report: FAIL")
    assert "**FAIL**" in text
    assert "npf_ratio by x: no_rows" in text


def test_write_report_writes_both_files(tmp_path: Path) -> None:
    json_path, md_path = report.write_report(build(), tmp_path / "2026-11-15")
    assert json.loads(json_path.read_text())["schema"] == report.SCHEMA
    assert md_path.read_text().startswith("# Makna eval report")


def test_the_default_directory_is_by_date_and_ignored_by_git() -> None:
    import datetime

    path = report.default_out_dir(datetime.date(2026, 11, 15))
    assert path.parts[-3:] == ("evals", "reports", "2026-11-15")
    ignore = (Path(__file__).resolve().parents[2] / ".gitignore").read_text().splitlines()
    assert "evals/reports/" in ignore
