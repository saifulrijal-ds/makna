"""WP-14C: the held-out retrieval split (EVL-1, EVL-2, SRC-2).

The file is checked against the definitions and the other questions; the tuning code is proven
unable to read it; the held-out run and its report are checked on made-up rankings.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from evals import heldout_eval, report, tune
from evals.golden.heldout import (
    HELDOUT_QUESTIONS_FILE,
    HeldoutFile,
    load_heldout_questions,
)
from evals.golden.reference import QUESTIONS_FILE, SEARCH_QUESTIONS_FILE
from evals.golden.schema import load_questions, load_search_questions
from makna.definitions.loader import load_definitions
from makna.model.definitions import Metric

from .eval_support import retrieval_section

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
DEFINITIONS = ROOT / "semantic" / "definitions"
HELDOUT_MODULES = {"evals.golden.heldout", "evals.heldout_eval"}


@pytest.fixture(scope="module")
def heldout() -> HeldoutFile:
    return load_heldout_questions()


@pytest.fixture(scope="module")
def metrics() -> dict[str, tuple[str, set[str]]]:
    """Approved metric name -> (status, every phrase that names it)."""
    found: dict[str, tuple[str, set[str]]] = {}
    for file in load_definitions(DEFINITIONS).valid_files():
        definition = file.definition
        if isinstance(definition, Metric):
            phrases = {definition.name, definition.name.replace("_", " "), definition.label}
            for values in (definition.synonyms or {}).values():
                phrases.update(values)
            found[definition.name] = (definition.status, {p.casefold() for p in phrases if p})
    return found


# ---- the file ----


def test_the_file_holds_one_english_and_one_indonesian_question_per_approved_metric(
    heldout: HeldoutFile, metrics: dict[str, tuple[str, set[str]]]
) -> None:
    approved = {name for name, (status, _) in metrics.items() if status == "approved"}
    questions = heldout.questions
    assert len(questions) >= 40
    assert Counter(q.language for q in questions) == {"en": len(approved), "id": len(approved)}
    for language in ("en", "id"):
        assert sorted(q.expected_metric for q in questions if q.language == language) == sorted(
            approved
        )


def test_the_file_states_who_wrote_the_questions_and_that_they_are_never_tuned_on(
    heldout: HeldoutFile,
) -> None:
    assert "never read the repository" in heldout.provenance
    assert "D-34" in heldout.provenance
    header = HELDOUT_QUESTIONS_FILE.read_text(encoding="utf-8").split("provenance:")[0]
    assert "NEVER TUNE ON THESE" in header


def singular(text: str) -> str:
    words = re.sub(r"[^\w]+", " ", text.casefold()).split()
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words)


def test_no_more_than_ten_questions_hold_a_phrase_of_their_own_metric(
    heldout: HeldoutFile, metrics: dict[str, tuple[str, set[str]]]
) -> None:
    """Ten of the 46 happened to (people use those words); the set is no more of a synonym lookup
    than that. A new synonym copied from these questions would raise the count: that is tuning on
    the split, and this test is where it shows."""
    with_phrase = [
        q.id
        for q in heldout.questions
        if any(
            f" {singular(p)} " in f" {singular(q.question)} " for p in metrics[q.expected_metric][1]
        )
    ]
    assert len(with_phrase) <= 10, with_phrase


def test_no_held_out_question_repeats_a_golden_or_search_question(heldout: HeldoutFile) -> None:
    texts = {q.question.casefold().strip() for q in load_questions(QUESTIONS_FILE)}
    texts |= {q.question.casefold().strip() for q in load_search_questions(SEARCH_QUESTIONS_FILE)}
    assert not [q.id for q in heldout.questions if q.question.casefold().strip() in texts]


def test_a_file_without_provenance_or_with_a_wrong_field_does_not_load(tmp_path: Path) -> None:
    path = tmp_path / "h.yaml"
    path.write_text("questions: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provenance"):
        load_heldout_questions(path)
    path.write_text(
        "provenance: " + "x" * 50 + "\nquestions:\n- {id: H01, language: fr, "
        "question: Quelle somme, expected_metric: npf_ratio}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="language"):
        load_heldout_questions(path)


# ---- tuning cannot read the held-out split ----


def imported_modules(path: Path) -> set[str]:
    """The `evals.*` modules a file imports, by dotted name."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.split(".")[0] == "evals"}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == "evals":
                found.add(node.module)
                found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


def module_file(name: str) -> Path | None:
    relative = Path(*name.split("."))
    for candidate in (ROOT / relative.with_suffix(".py"), ROOT / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def closure(start: str) -> dict[str, Path]:
    """Every module under `evals` that `start` imports, directly or through another, and the
    package `__init__` files on the way."""
    seen: dict[str, Path] = {}
    todo = [start]
    while todo:
        name = todo.pop()
        path = module_file(name)
        if name in seen or path is None:
            continue
        seen[name] = path
        parts = name.split(".")
        todo += [".".join(parts[:i]) for i in range(1, len(parts))]
        todo += sorted(imported_modules(path))
    return seen


def test_the_tuning_imports_nothing_that_can_reach_the_held_out_file() -> None:
    reachable = closure("evals.tune")
    assert "evals.tune" in reachable and "evals.retrieval" in reachable  # the walk works
    assert not HELDOUT_MODULES & set(reachable), sorted(HELDOUT_MODULES & set(reachable))
    for name, path in reachable.items():
        text = path.read_text(encoding="utf-8")
        assert "heldout_questions" not in text, name
        assert "HELDOUT_QUESTIONS_FILE" not in text, name


def test_the_check_detects_a_module_that_imports_the_held_out_loader(tmp_path: Path) -> None:
    source = tmp_path / "x.py"
    source.write_text("from evals.golden.heldout import load_heldout_questions\n")
    assert "evals.golden.heldout" in imported_modules(source)
    source.write_text("from evals.golden import heldout\n")
    assert "evals.golden.heldout" in imported_modules(source)
    source.write_text("from evals import heldout_eval\n")
    assert "evals.heldout_eval" in imported_modules(source)


def test_loading_the_tuned_questions_does_not_open_the_held_out_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    real_read_text = Path.read_text

    def guarded(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(self.name)
        if self.name == HELDOUT_QUESTIONS_FILE.name:
            raise AssertionError("the tuning read the held-out file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    questions, search_questions = tune.load_tuned_questions()
    assert len(questions) >= 50 and len(search_questions) >= 16
    assert sorted(set(opened)) == [QUESTIONS_FILE.name, SEARCH_QUESTIONS_FILE.name]


# ---- the held-out run and report ----


def detail(language: str, band: str, found: bool) -> dict[str, Any]:
    return {"language": language, "confidence": band, "found_at_3": {"fused": found}}


def test_the_band_summary_counts_valid_questions_that_come_back_low() -> None:
    details = [
        detail("en", "high", True),
        detail("en", "low", True),
        detail("en", "low", False),
        detail("id", "medium", True),
        detail("id", "low", True),
    ]
    summary = heldout_eval.band_summary(details)
    assert summary["n"] == 5
    assert summary["bands"] == {"high": 1, "medium": 1, "low": 3}
    assert summary["low_found_at_3"] == 2
    assert summary["by_language"]["en"]["bands"]["low"] == 2
    assert summary["by_language"]["en"]["low_found_at_3"] == 1
    assert summary["by_language"]["id"]["low_found_at_3"] == 1
    assert summary["found_at_3_by_band"]["high"] == 1


def with_missed_question(section: dict[str, Any]) -> dict[str, Any]:
    """The fixture section with one held-out question that was not found and came back `low`."""
    question = {
        "id": "H01",
        "question": "q",
        "language": "en",
        "expected": ["npf_ratio"],
        "confidence": "low",
        "coverage": 0.1,
        "top_cosine": 0.4,
        "ranked_ids": ["metric:x"],
        "rankings": {"fused": ["x"], "keyword": ["x"], "vector": ["x"]},
        "found_at_3": {"fused": False, "keyword": False, "vector": False},
        "reciprocal_rank": {"fused": 0.0, "keyword": 0.0, "vector": 0.0},
    }
    heldout = {**section["heldout"], "questions": [question]}
    return {**section, "heldout": heldout}


def test_the_report_shows_the_tuned_and_the_held_out_split_side_by_side() -> None:
    document = {
        "schema": report.SCHEMA,
        "generated_at": "2026-11-16T00:00:00+00:00",
        "command": "retrieval",
        "settings": {
            "git": {"commit": "abc", "dirty": False},
            "today": "2026-07-01",
            "search": {
                "w_keyword": 1.0,
                "w_vector": 1.0,
                "candidates": 30,
                "confidence": {"high": 0.6, "low": 0.3, "low_cosine": None},
            },
            "embedding": {"provider": "hash", "model": "m", "dimension": 8},
            "governance": {"mode": "strict", "min_group_size": 5},
            "dialect": "postgres",
            "index": {"embedding_model": "m", "documents": 1, "definitions_version": "v"},
        },
        "retrieval": with_missed_question(retrieval_section()),
        "gates": [],
        "passed": True,
    }
    text = report.render_markdown(document)
    assert "### Tuned and held-out splits" in text
    assert "| tuned (golden) | 10 |" in text and "| held-out | 10 |" in text
    assert "Confidence bands of the held-out questions" in text
    assert "never read the repository" in text
    assert "Held-out questions not found in the first 3" in text and "H01" in text
