"""The report: JSON is the source of truth, Markdown is rendered from it.

`evals/reports/<date>/report.json` and `report.md` (git-ignored). The JSON holds the settings the
run used, every score, the per-question retrieval detail, the items of each compile part, and the
gate results, so two reports can be compared and a failure can be traced to its question.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evals.gates import GateResult
from evals.scores import LANGUAGES, SYSTEMS

SCHEMA = "makna-eval-report/v1"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def default_out_dir(today: datetime.date | None = None) -> Path:
    return REPORTS_DIR / (today or datetime.date.today()).isoformat()


def build_report(
    command: str,
    settings: Mapping[str, Any],
    results: Sequence[GateResult],
    *,
    retrieval: Mapping[str, Any] | None = None,
    compile_section: Mapping[str, Any] | None = None,
    resolver: Mapping[str, Any] | None = None,
    generated_at: datetime.datetime | None = None,
) -> dict[str, Any]:
    moment = generated_at or datetime.datetime.now(datetime.UTC)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": moment.isoformat(timespec="seconds"),
        "command": command,
        "settings": dict(settings),
    }
    if retrieval is not None:
        report["retrieval"] = dict(retrieval)
    if compile_section is not None:
        report["compile"] = dict(compile_section)
    if resolver is not None:
        report["resolver"] = dict(resolver)
    report["gates"] = [
        {
            "name": r.name,
            "label": r.label,
            "op": r.op,
            "threshold": r.threshold,
            "value": r.value,
            "passed": r.passed,
            "failures": list(r.failures),
        }
        for r in results
    ]
    report["passed"] = all(r.passed for r in results)
    return report


def _num(value: object, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    return f"{float(value):.{digits}f}"  # type: ignore[arg-type]


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    verdict = "PASS" if report["passed"] else "FAIL"
    settings = report["settings"]
    lines += [f"# Makna eval report: {verdict}", ""]
    lines += [
        f"Command `{report['command']}`, generated {report['generated_at']}. "
        f"Commit `{settings['git']['commit']}`"
        + (" (uncommitted changes)" if settings["git"].get("dirty") else "")
        + f". Today for every question: {settings['today']}.",
        "",
    ]
    lines += ["## Gates", ""]
    gate_rows = [
        [
            g["label"],
            _num(g["value"]),
            f"{g['op']} {_num(g['threshold'])}",
            "pass" if g["passed"] else "**FAIL**",
        ]
        for g in report["gates"]
    ]
    lines += _table(["Gate", "Value", "Required", "Result"], gate_rows) + [""]
    for gate in report["gates"]:
        if gate["failures"]:
            lines += [f"Failing for {gate['label']}:", ""]
            lines += [f"- {entry}" for entry in gate["failures"]] + [""]
    if "retrieval" in report:
        lines += _render_retrieval(report["retrieval"])
    if "compile" in report:
        lines += _render_compile(report["compile"])
    if "resolver" in report:
        lines += _render_resolver(report["resolver"])
    lines += _render_settings(settings)
    return "\n".join(lines).rstrip() + "\n"


def _render_by_kind(by_kind: Mapping[str, Any] | None) -> list[str]:
    """Recall at 3 per kind of object (WP-15D). Reported, not gated."""
    if not by_kind:
        return []
    lines = [
        "### Recall at 3 by kind of object",
        "",
        f"Searches with no `kinds` ({', '.join(by_kind['kinds'])}), limit {by_kind['limit']}, as "
        "an agent makes them. No gate. The `metric` row repeats the golden questions over this "
        "wider search (the gated figure above searches `metric` and `glossary_term` only); the "
        "other rows come from `evals/golden/search_questions.yaml`.",
        "",
    ]
    rows = [
        [r["kind"], _num(r["recall_at_3"]), _num(r["n_questions"]), r["source"]]
        for r in by_kind["rows"]
    ]
    lines += _table(["Kind", "Recall at 3", "Questions", "Source"], rows) + [""]
    missed = [q for q in by_kind["questions"] if not q["found_at_3"]]
    if missed:
        lines += ["Not found in the first 3:", ""]
        lines += _table(
            ["Question", "Kind", "Expected", "Rank", "First hits"],
            [
                [
                    q["id"],
                    q["kind"],
                    ", ".join(q["expected"]),
                    "none in 10" if q["rank"] is None else str(q["rank"]),
                    ", ".join(q["ranked_ids"][:3]),
                ]
                for q in missed
            ],
        ) + [""]
    return lines


def _split_row(name: str, n: object, scores: Mapping[str, Any]) -> list[str]:
    fused = scores["fused"]
    return [
        name,
        _num(n),
        _num(fused["recall_at_3"]),
        _num(fused["recall_at_1"]),
        _num(fused["mrr"]),
        _num(fused["by_language"]["en"]["recall_at_3"]),
        _num(fused["by_language"]["id"]["recall_at_3"]),
        _num(fused["false_abstention_rate"]),
    ]


def _render_splits(section: Mapping[str, Any]) -> list[str]:
    """The tuned and the held-out split side by side (WP-14C), with the held-out detail."""
    heldout = section.get("heldout")
    if not heldout:
        return []
    lines = [
        "### Tuned and held-out splits",
        "",
        "The fused search over each split. The tuned split is the golden set, which the weights "
        "were tuned on and which carries the gates above; the held-out split "
        "(`evals/golden/heldout_questions.yaml`) was never tuned on. Recall at 1 and mean "
        "reciprocal rank are over `answer` questions; the last column is the share of valid "
        "questions whose confidence band is `low`.",
        "",
        "Held-out provenance: " + heldout["provenance"],
        "",
    ]
    rows = [
        _split_row("tuned (golden)", section["n_questions"], section["scores"]),
        _split_row("held-out", heldout["n_questions"], heldout["scores"]),
    ]
    lines += _table(
        [
            "Split",
            "Questions",
            "Recall at 3",
            "Recall at 1",
            "MRR",
            "Recall at 3 en",
            "Recall at 3 id",
            "Low band",
        ],
        rows,
    ) + [""]
    system_rows = [
        [
            name,
            _num(heldout["scores"][name]["recall_at_3"]),
            _num(heldout["scores"][name]["recall_at_1"]),
            _num(heldout["scores"][name]["mrr"]),
            _num(heldout["scores"][name]["by_language"]["en"]["recall_at_3"]),
            _num(heldout["scores"][name]["by_language"]["id"]["recall_at_3"]),
        ]
        for name in SYSTEMS
    ]
    lines += ["Held-out split, each search:", ""]
    lines += _table(
        ["Search", "Recall at 3", "Recall at 1", "MRR", "Recall at 3 en", "Recall at 3 id"],
        system_rows,
    ) + [""]
    confidence = heldout["confidence"]
    band_rows = []
    for label, part in (
        ("all", confidence),
        ("en", confidence["by_language"]["en"]),
        ("id", confidence["by_language"]["id"]),
    ):
        band_rows.append(
            [
                label,
                _num(part["n"]),
                _num(part["bands"]["high"]),
                _num(part["bands"]["medium"]),
                _num(part["bands"]["low"]),
                _num(part["low_found_at_3"]),
            ]
        )
    lines += [
        "Confidence bands of the held-out questions (every one has a defined metric, so a `low` "
        "band is a false abstention; the last column counts the `low` ones whose right metric "
        "is in the first 3 hits):",
        "",
    ]
    lines += _table(
        ["Language", "Questions", "high", "medium", "low", "low with the metric in the first 3"],
        band_rows,
    ) + [""]
    missed = [q for q in heldout["questions"] if not q["found_at_3"]["fused"]]
    if missed:
        lines += ["Held-out questions not found in the first 3:", ""]
        lines += _table(
            ["Id", "Language", "Expected", "Fused top 3", "Confidence"],
            [
                [
                    q["id"],
                    q["language"],
                    ", ".join(q["expected"]),
                    ", ".join(q["rankings"]["fused"][:3]) or "none",
                    q["confidence"],
                ]
                for q in missed
            ],
        ) + [""]
    return lines


def _render_retrieval(section: Mapping[str, Any]) -> list[str]:
    scores = section["scores"]
    lines = ["## Retrieval (EVL-2)", ""]
    lines += [
        f"{section['n_questions']} questions, kinds {', '.join(section['kinds'])}, "
        f"limit {section['limit']}, role {section['role']}. "
        "Recall at 3 is over `answer` and `clarify` questions (every candidate of a `clarify` "
        "question must be in the first 3); recall at 1 and mean reciprocal rank are over `answer` "
        "questions.",
        "",
    ]
    rows = [
        [
            name,
            _num(scores[name]["recall_at_3"]),
            _num(scores[name]["recall_at_1"]),
            _num(scores[name]["mrr"]),
        ]
        for name in SYSTEMS
    ]
    lines += _table(["Search", "Recall at 3", "Recall at 1", "MRR"], rows) + [""]
    fused = scores["fused"]
    lines += _table(
        ["Confidence score", "Value", "Questions"],
        [
            [
                "Abstention recall (`no_metric`, no definition)",
                _num(fused["abstention_recall"]),
                _num(fused["n_no_definition"]),
            ],
            [
                "False abstention rate (`answer`)",
                _num(fused["false_abstention_rate"]),
                _num(fused["n_answer_confidence"]),
            ],
        ],
    ) + [""]
    lines += _render_splits(section)
    lines += _render_by_kind(section.get("by_kind"))
    lines += ["### By language", ""]
    language_rows: list[list[str]] = []
    for language in LANGUAGES:
        for name in SYSTEMS:
            part = scores[name]["by_language"][language]
            language_rows.append(
                [
                    language,
                    name,
                    _num(part["recall_at_3"]),
                    _num(part["recall_at_1"]),
                    _num(part["mrr"]),
                    _num(part["n_recall_at_3"]),
                ]
            )
    lines += _table(
        ["Language", "Search", "Recall at 3", "Recall at 1", "MRR", "Questions (recall)"],
        language_rows,
    ) + [""]
    latency = section["latency"]
    lines += [
        "### Latency (NFR-2)",
        "",
        f"{latency['n']} timed searches ({latency['rounds']} rounds of the fused search): "
        f"p50 {_num(latency['p50_ms'], 1)} ms, p95 {_num(latency['p95_ms'], 1)} ms, "
        f"max {_num(latency['max_ms'], 1)} ms. The first search, which loads the embedding "
        f"model, took {_num(latency['warmup_ms'], 0)} ms and is not in these figures.",
        "",
    ]
    misses = [
        q
        for q in section["questions"]
        if (q["outcome"] in ("answer", "clarify") and not q["found_at_3"]["fused"])
        or (q["outcome"] == "answer" and q["confidence"] == "low")
        or (q["outcome"] == "no_metric" and q["reason"] == "no_definition")
    ]
    if misses:
        lines += ["### Questions to read", ""]
        lines += [
            "Not found in the first 3, answered with low confidence, or a question with no "
            "definition (to see its confidence band).",
            "",
        ]
        rows = [
            [
                q["id"],
                q["outcome"],
                ", ".join(q["expected"]) or "none",
                ", ".join(q["rankings"]["fused"][:3]) or "none",
                q["confidence"],
                f"{_num(q['coverage'], 2)}",
                _num(q["top_cosine"], 2),
            ]
            for q in misses
        ]
        lines += _table(
            ["Id", "Outcome", "Expected", "Fused top 3", "Confidence", "Coverage", "Top cosine"],
            rows,
        ) + [""]
    return lines


def _render_compile(section: Mapping[str, Any]) -> list[str]:
    lines = ["## Compile correctness (EVL-3)", ""]
    names = {
        "part_a": "A: golden requests (answer questions and clarify candidates)",
        "part_b": "B: every approved metric, plain, at its default grain, and by each dimension",
        "part_c": "C: refusals (expected error codes)",
    }
    rows = []
    for key, label in names.items():
        part = section[key]
        rows.append([label, _num(part["passed"]), _num(part["total"]), _num(part["pass_rate"])])
    lines += _table(["Part", "Passed", "Requests", "Pass rate"], rows) + [""]
    for key, label in names.items():
        failures = section[key]["failures"]
        if failures:
            lines += [f"Failures in part {label.split(':')[0]}:", ""]
            lines += [f"- {entry['id']}: {entry['reason']}" for entry in failures] + [""]
    plain = section.get("part_b_without_default_grain")
    if plain:
        lines += [
            "Metrics with no default grain (no part B request at a grain): "
            + ", ".join(plain)
            + ".",
            "",
        ]
    return lines


def _render_resolver(section: Mapping[str, Any]) -> list[str]:
    """The resolver's exact-request accuracy (WP-20). Informative: no gate."""
    answer, first = section["answer"], section["answer_first_20"]
    lines = ["## Resolver (rule-based, no gate)", ""]
    lines += [
        f"The rule-based resolver of section 8.4 reads each golden question with today = "
        f"{section['today']} and builds a request. A request is exact when it equals "
        "`expected.request` as a value (metric, dimensions, filters, grain, both dates). The "
        "held-out questions are not used.",
        "",
    ]
    rows = [
        [
            "`answer`, all",
            f"{_num(answer['exact'])} of {_num(answer['total'])}",
            _num(answer["rate"]),
            _status_line(answer),
        ],
        [
            "`answer`, G01 to G20",
            f"{_num(first['exact'])} of {_num(first['total'])}",
            _num(first["rate"]),
            _status_line(first),
        ],
    ]
    for language, label in (("en", "English"), ("id", "Indonesian"), ("mixed", "mixed")):
        part = section["by_language"][language]
        if part["total"]:
            rows.append(
                [
                    f"`answer`, {label}",
                    f"{_num(part['exact'])} of {_num(part['total'])}",
                    _num(part["rate"]),
                    _status_line(part),
                ]
            )
    clarify, none = section["clarify"], section["no_answer"]
    rows += [
        [
            "`clarify`: ambiguous, with the expected candidates",
            f"{_num(clarify['exact'])} of {_num(clarify['total'])}",
            _num(clarify["rate"]),
            _status_line(clarify),
        ],
        [
            "`no_metric` and `refuse`: no request built",
            f"{_num(none['exact'])} of {_num(none['total'])}",
            _num(none["rate"]),
            _status_line(none),
        ],
    ]
    lines += _table(["Questions", "Exact", "Rate", "Resolver status of each"], rows) + [""]
    lines += [
        "G01 to G20 are the first 20 questions of the golden set (WP-20 acceptance 2: at least "
        "10 of the 15 `answer` questions among them).",
        "",
    ]
    wrong = [i for i in section["items"] if i["outcome"] == "answer" and not i["exact"]]
    if wrong:
        lines += ["`answer` questions whose request is not exact:", ""]
        lines += _table(
            ["Id", "Status", "Expected metric", "Built metric", "Unresolved words"],
            [
                [
                    i["id"],
                    i["status"],
                    (i["expected_request"] or {}).get("metric", ""),
                    (i["request"] or {}).get("metric", "none"),
                    ", ".join(i["unresolved_terms"]) or "none",
                ]
                for i in wrong
            ],
        ) + [""]
    return lines


def _status_line(part: Mapping[str, Any]) -> str:
    by = part["by_status"]
    return f"resolved {by['resolved']}, ambiguous {by['ambiguous']}, no match {by['no_match']}"


def _render_settings(settings: Mapping[str, Any]) -> list[str]:
    search = settings["search"]
    embedding = settings["embedding"]
    governance = settings["governance"]
    index = settings["index"]
    confidence = search["confidence"]
    lines = ["## Settings used", ""]
    rows = [
        ["w_keyword, w_vector", f"{search['w_keyword']}, {search['w_vector']}"],
        ["candidates per leg", str(search["candidates"])],
        [
            "confidence high, low, low_cosine",
            f"{confidence['high']}, {confidence['low']}, {confidence['low_cosine']}",
        ],
        ["embedding provider, model", f"{embedding['provider']}, {embedding['model']}"],
        ["embedding dimension", str(embedding["dimension"])],
        [
            "governance mode, minimum group size",
            f"{governance['mode']}, {governance['min_group_size']}",
        ],
        ["dialect", settings["dialect"]],
        ["index model, documents", f"{index['embedding_model']}, {index['documents']}"],
        ["definitions version", str(index["definitions_version"])],
    ]
    lines += _table(["Setting", "Value"], rows) + [""]
    return lines


def write_report(report: Mapping[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    md_path = out_dir / "report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path
