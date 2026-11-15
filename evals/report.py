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
    lines += _render_settings(settings)
    return "\n".join(lines).rstrip() + "\n"


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
