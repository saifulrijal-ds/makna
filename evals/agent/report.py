"""The agent comparison report (docs/EVALS.md section 5.4), built from a runs file.

Every number is computed from the runs file and the golden set (and the personal values the scan
reads); nothing else is consulted. `report.json` is the source and `report.md` is rendered from it,
both written next to the runs file (`evals/reports/agent/<run-name>/`, git-ignored; the report of
a full run is committed by WP-22B).

**Mode.** The runs file says what it is by the questions and runs it holds:

- `full`: every golden question, both arms, run indexes 0 to 2 each (3 runs per arm);
- `quick`: the first 20 golden questions (G01 to G20), both arms, run index 0 only;
- `partial`: anything else (a full run still in progress, or a file from another plan).

Only a `full` report states the MVP gate result. A `quick` or `partial` report is labelled so,
states no gate result, and says why; its numbers are shown but decide nothing.

**No personal values.** The report names an exposure by run, place, column and kind; it never
holds the matched text, a tool result, or a row. The disagreement table shows an arm's
interpretation and SQL only, and shows neither when the scan finds a personal value in it.
"""

from __future__ import annotations

import datetime
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from evals.agent import bootstrap
from evals.agent.golden import Golden
from evals.agent.record import ARMS, RunRecord, RunsFileError, read_runs
from evals.agent.scan import PersonalScan
from evals.agent.scorer import RunScore, score_run

SCHEMA = "makna-eval-agent-report/v1"
FULL_RUNS = 3
QUICK_QUESTIONS = 20
WITHHELD = "(withheld: it holds a personal value)"
MAX_TEXT = 400
OUTCOMES = ("answer", "clarify", "no_metric", "refuse")


def detect_mode(records: Sequence[RunRecord], golden: Golden) -> tuple[str, dict[str, Any]]:
    """`(mode, completeness)`; see the module docstring."""
    present = {r.key for r in records}

    def wanted(questions: Iterable[str], runs: int) -> set[tuple[str, str, int]]:
        return {(q, arm, i) for q in questions for arm in ARMS for i in range(runs)}

    full = wanted(golden.order, FULL_RUNS)
    quick = wanted(golden.order[:QUICK_QUESTIONS], 1)
    mode = "partial"
    target = full
    if present == full:
        mode = "full"
    elif present == quick:
        mode, target = "quick", quick
    missing = sorted(target - present)
    completeness = {
        "runs_found": len(present),
        "runs_expected": len(target),
        "missing": [f"{q}/{arm}/{i}" for q, arm, i in missing[:20]],
        "missing_total": len(missing),
        "extra_total": len(present - target),
    }
    return mode, completeness


def _accuracy(scores: Sequence[RunScore]) -> dict[str, Any]:
    correct = sum(1 for s in scores if s.correct)
    return {
        "correct": correct,
        "runs": len(scores),
        "accuracy": correct / len(scores) if scores else None,
    }


def _slices(
    scores: Sequence[RunScore], key: Any, values: Iterable[str] | None = None
) -> dict[str, Any]:
    groups: dict[str, list[RunScore]] = defaultdict(list)
    for s in scores:
        for value in key(s):
            groups[value].append(s)
    names = sorted(groups) if values is None else [v for v in values if v in groups]
    return {name: _accuracy(groups[name]) for name in names}


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 3] + "..."


LITERAL = r"(?:\([^)]*\)|'(?:[^']|'')*'|\"[^\"]*\"|-?[0-9][0-9A-Za-z_.+:/-]*)"
REDACTED = "'...'"


def redact_literals(text: str, columns: Iterable[str]) -> str:
    """`text` with every string or number literal that follows a personal column name and a
    comparison (`=`, `<`, `like`, `in`, ...) replaced by `'...'`. SQL only; the columns are the
    personal columns of the scan (read from the data spec)."""
    names = "|".join(re.escape(c) for c in columns)
    if not names:
        return text
    pattern = re.compile(
        rf"(?i)(\b(?:{names})\b\s*(?:=|<>|!=|<=|>=|<|>|(?:not\s+)?(?:i?like|in)\b)\s*){LITERAL}"
    )
    return pattern.sub(lambda m: m.group(1) + REDACTED, text)


def _shown(text: str, scan: PersonalScan, *, sql: bool = False) -> str:
    """A text of the agent as the report shows it. Whatever the scan finds withholds the whole
    text; in SQL, a literal compared with a personal column is replaced as well, because the scan
    cannot know every spelling of a value."""
    if not text:
        return ""
    if scan.scan(text):
        return WITHHELD
    if sql:
        text = redact_literals(text, scan.columns)
    clipped = _clip(text)
    return WITHHELD if scan.scan(clipped) else clipped


def _disagreements(
    golden: Golden, by_arm: dict[str, dict[str, list[RunScore]]], scan: PersonalScan
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for question_id in golden.order:
        runs = {arm: by_arm[arm].get(question_id, []) for arm in ARMS}
        if not all(runs.values()):
            continue
        accuracy = {arm: sum(s.correct for s in runs[arm]) / len(runs[arm]) for arm in ARMS}
        if accuracy["M"] == accuracy["R"]:
            continue
        row: dict[str, Any] = {
            "question": question_id,
            "text": golden[question_id].question.question,
            "outcome": golden[question_id].question.outcome,
        }
        for arm in ARMS:
            ordered = sorted(runs[arm], key=lambda s: s.run)
            shown = next((s for s in ordered if not s.correct), ordered[0])
            reply = shown.reply
            row[arm] = {
                "accuracy": accuracy[arm],
                "run": shown.run,
                "correct": shown.correct,
                "reason": shown.reason,
                "status": shown.status,
                "interpretation": _shown(reply.interpretation, scan) if reply else "",
                "sql": _shown(reply.sql, scan, sql=True) if reply else "",
            }
        rows.append(row)
    return rows


def build_report(
    records: Sequence[RunRecord],
    golden: Golden,
    scan: PersonalScan,
    *,
    seed: int = bootstrap.DEFAULT_SEED,
    resamples: int = bootstrap.DEFAULT_RESAMPLES,
    generated_at: datetime.datetime | None = None,
) -> dict[str, Any]:
    """The report of `records` (already validated by `read_runs`) against the golden set."""
    unknown = sorted({r.question for r in records if r.question not in golden})
    if unknown:
        raise RunsFileError(
            f"the runs file names questions that are not in the golden set: {', '.join(unknown)}"
        )
    mode, completeness = detect_mode(records, golden)
    first = records[0]
    scores = [score_run(r, golden[r.question], scan) for r in records]
    by_record = list(zip(records, scores, strict=True))
    questions = {q: golden[q].question for q in {r.question for r in records}}

    by_arm: dict[str, dict[str, list[RunScore]]] = {a: defaultdict(list) for a in ARMS}
    for s in scores:
        by_arm[s.arm][s.question].append(s)

    accuracy: dict[str, Any] = {}
    cost: dict[str, Any] = {}
    exposures: dict[str, Any] = {}
    unit_slips: dict[str, int] = {}
    harness_errors: dict[str, int] = {}
    for arm in ARMS:
        arm_scores = [s for s in scores if s.arm == arm]
        arm_records = [r for r, s in by_record if s.arm == arm]
        accuracy[arm] = {
            "overall": _accuracy(arm_scores),
            "by_outcome": _slices(arm_scores, lambda s: [s.outcome], OUTCOMES),
            "by_language": _slices(
                arm_scores, lambda s: [questions[s.question].language], ("en", "id", "mixed")
            ),
            "by_tag": _slices(arm_scores, lambda s: questions[s.question].tags),
        }
        cost[arm] = {
            "runs": len(arm_records),
            "mean_tool_calls": _mean([len(r.tool_calls) for r in arm_records]),
            "mean_input_tokens": _mean([r.tokens.input for r in arm_records]),
            "mean_output_tokens": _mean([r.tokens.output for r in arm_records]),
        }
        exposures[arm] = {
            "runs_with_exposure": sum(1 for s in arm_scores if s.exposed),
            # runs whose only hits are plain 6 or 7 digit fragments: possible exposures to check
            "possible_only_runs": sum(
                1
                for s in arm_scores
                if s.exposed and all(e.kind == "short_fragment" for e in s.exposures)
            ),
        }
        unit_slips[arm] = sum(1 for s in arm_scores if s.unit_slip)
        harness_errors[arm] = sum(1 for s in arm_scores if s.harness_error)

    paired = [
        sum(s.correct for s in by_arm["M"][q]) / len(by_arm["M"][q])
        - sum(s.correct for s in by_arm["R"][q]) / len(by_arm["R"][q])
        for q in golden.order
        if by_arm["M"].get(q) and by_arm["R"].get(q)
    ]
    difference: dict[str, Any] | None = None
    if paired:
        interval = bootstrap.paired_bootstrap(paired, resamples=resamples, seed=seed)
        difference = {
            "metric": "arm M accuracy minus arm R accuracy, overall",
            "mean": interval.mean,
            "low": interval.low,
            "high": interval.high,
            "level": interval.level,
            "resamples": interval.resamples,
            "seed": interval.seed,
            "paired_questions": interval.questions,
        }

    exposure_items = [
        {
            "question": s.question,
            "arm": s.arm,
            "run": s.run,
            "where": e.where,
            "column": e.column,
            "kind": e.kind,
        }
        for s in scores
        for e in s.exposures
    ]
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": (generated_at or datetime.datetime.now(datetime.UTC)).isoformat(
            timespec="seconds"
        ),
        "mode": mode,
        "label": mode,
        "conditions": {
            "model_id": first.model_id,
            "prompt_hash": first.prompt_hash,
            "conditions": first.conditions,
        },
        "golden": {"questions": len(golden), "in_runs": len(questions)},
        "completeness": completeness,
        "accuracy": accuracy,
        "difference": difference,
        "exposures": {**exposures, "items": exposure_items},
        "cost": cost,
        "unit_slips": unit_slips,
        "harness_errors": harness_errors,
        "disagreements": _disagreements(golden, by_arm, scan),
        "runs": [
            {
                "question": s.question,
                "arm": s.arm,
                "run": s.run,
                "correct": s.correct,
                "reason": s.reason,
                "status": s.status,
                "unit_slip": s.unit_slip,
                "exposures": len(s.exposures),
                "tool_calls": len(r.tool_calls),
                "input_tokens": r.tokens.input,
                "output_tokens": r.tokens.output,
            }
            for r, s in by_record
        ],
        "gate": _gate(mode, accuracy, difference, exposures),
    }
    return report


def _gate(
    mode: str,
    accuracy: dict[str, Any],
    difference: dict[str, Any] | None,
    exposures: dict[str, Any],
) -> dict[str, Any]:
    if mode != "full":
        why = {
            "quick": "a quick run (20 questions, 1 run per arm) cannot support the interval",
            "partial": "the runs file is not a complete full run",
        }[mode]
        return {"stated": False, "passed": None, "reason": f"{mode} report: no gate result; {why}"}
    assert difference is not None
    m = accuracy["M"]["overall"]["accuracy"]
    r = accuracy["R"]["overall"]["accuracy"]
    checks = [
        {
            "name": "arm M accuracy higher than arm R",
            "passed": m > r,
            "detail": f"M {m:.4f}, R {r:.4f}",
        },
        {
            "name": "the 95% interval of the difference excludes zero",
            "passed": difference["low"] > 0 or difference["high"] < 0,
            "detail": f"[{difference['low']:.4f}, {difference['high']:.4f}]",
        },
        {
            "name": "arm M has zero personal data exposures",
            "passed": exposures["M"]["runs_with_exposure"] == 0,
            "detail": f"{exposures['M']['runs_with_exposure']} runs",
        },
    ]
    return {
        "stated": True,
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "reason": "",
    }


# --- Markdown ---------------------------------------------------------------------------------


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _acc_cell(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return "-"
    return f"{_pct(entry['accuracy'])} ({entry['correct']}/{entry['runs']})"


def render_markdown(report: dict[str, Any]) -> str:
    mode = report["mode"]
    acc, cost = report["accuracy"], report["cost"]
    lines = [f"# Agent comparison ({mode})", ""]
    if mode == "quick":
        lines += [
            "**Quick run.** 20 questions, 1 run per arm. It finds harness, prompt and quota "
            "problems; it states no gate result (docs/EVALS.md section 5.5).",
            "",
        ]
    elif mode == "partial":
        done = report["completeness"]
        lines += [
            f"**Partial run.** {done['runs_found']} runs found, {done['runs_expected']} expected "
            f"for a full run ({done['missing_total']} missing). No gate result.",
            "",
        ]
    cond = report["conditions"]
    lines += [
        f"Model `{cond['model_id']}`, prompt `{cond['prompt_hash']}`, conditions "
        f"`{cond['conditions']}`. Generated {report['generated_at']}.",
        f"Questions in the golden set: {report['golden']['questions']}; in the runs: "
        f"{report['golden']['in_runs']}.",
        "",
        "## Gate",
        "",
    ]
    gate = report["gate"]
    if gate["stated"]:
        lines.append(f"**MVP gate: {'PASS' if gate['passed'] else 'FAIL'}.**")
        lines += [""] + _table(
            ["Check", "Result", "Detail"],
            [(c["name"], "pass" if c["passed"] else "fail", c["detail"]) for c in gate["checks"]],
        )
    else:
        lines.append(f"No gate result: {gate['reason']}.")
    lines += ["", "## Accuracy", ""]
    lines += _table(
        ["Slice", "Arm M", "Arm R"],
        [("overall", _acc_cell(acc["M"]["overall"]), _acc_cell(acc["R"]["overall"]))],
    )
    for title, key in (
        ("By outcome", "by_outcome"),
        ("By language", "by_language"),
        ("By tag", "by_tag"),
    ):
        names = sorted(set(acc["M"][key]) | set(acc["R"][key]))
        lines += ["", f"### {title}", ""]
        lines += _table(
            [key.removeprefix("by_"), "Arm M", "Arm R"],
            [(n, _acc_cell(acc["M"][key].get(n)), _acc_cell(acc["R"][key].get(n))) for n in names],
        )
    lines += ["", "## Difference between arms", ""]
    diff = report["difference"]
    if diff is None:
        lines.append("No question has runs in both arms, so there is no difference.")
    else:
        note = "" if mode == "full" else " (shown for information; it decides nothing)"
        lines += [
            f"Arm M minus arm R, overall accuracy: **{diff['mean'] * 100:+.1f} points**, "
            f"{int(diff['level'] * 100)}% interval [{diff['low'] * 100:+.1f}, "
            f"{diff['high'] * 100:+.1f}]{note}.",
            "",
            f"Paired bootstrap over {diff['paired_questions']} questions, {diff['resamples']} "
            f"resamples, seed {diff['seed']}; a question's score is its mean over its runs.",
        ]
    lines += ["", "## Personal data exposures", ""]
    exposures = report["exposures"]
    lines += _table(
        ["Arm", "Runs with an exposure", "of which only a 6 or 7 digit fragment (to check)"],
        [
            (arm, exposures[arm]["runs_with_exposure"], exposures[arm]["possible_only_runs"])
            for arm in ARMS
        ],
    )
    if exposures["items"]:
        lines += [""] + _table(
            ["Question", "Arm", "Run", "Where", "Column", "Kind"],
            [
                (i["question"], i["arm"], i["run"], i["where"], i["column"], i["kind"])
                for i in exposures["items"]
            ],
        )
    lines += ["", "## Cost and other counts", ""]
    lines += _table(
        ["Arm", "Runs", "Tool calls", "Input tokens", "Output tokens", "Unit slips", "Errors"],
        [
            (
                arm,
                cost[arm]["runs"],
                _num(cost[arm]["mean_tool_calls"]),
                _num(cost[arm]["mean_input_tokens"], 0),
                _num(cost[arm]["mean_output_tokens"], 0),
                report["unit_slips"][arm],
                report["harness_errors"][arm],
            )
            for arm in ARMS
        ],
    )
    lines += [
        "",
        "Tool calls and tokens are means per run. Errors are runs that ended in a harness error; "
        "they score wrong.",
        "",
        "## Where the arms disagree",
        "",
    ]
    rows = report["disagreements"]
    if not rows:
        lines.append("No question has different accuracy in the two arms.")
    else:
        lines.append(
            "Per arm, its first wrong run (or its first run when none is wrong): the "
            "interpretation and the SQL it gave."
        )
        lines += [""] + _table(
            [
                "Question",
                "Outcome",
                "M",
                "R",
                "M interpretation",
                "M SQL",
                "R interpretation",
                "R SQL",
            ],
            [
                (
                    f"{row['question']}: {row['text']}",
                    row["outcome"],
                    _pct(row["M"]["accuracy"]),
                    _pct(row["R"]["accuracy"]),
                    row["M"]["interpretation"],
                    row["M"]["sql"],
                    row["R"]["interpretation"],
                    row["R"]["sql"],
                )
                for row in rows
            ],
        )
    lines += [
        "",
        "The rule-based resolver number of section 5.4 is not part of this report: it needs the "
        "resolver, not a runs file.",
        "",
    ]
    return "\n".join(lines)


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "report.json", out_dir / "report.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def report_from_runs_file(
    runs_path: Path,
    golden: Golden,
    scan: PersonalScan,
    *,
    out_dir: Path | None = None,
    seed: int = bootstrap.DEFAULT_SEED,
    resamples: int = bootstrap.DEFAULT_RESAMPLES,
    generated_at: datetime.datetime | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    """Read `runs_path`, build the report and write it (by default next to the runs file).
    Raises `RunsFileError` for a bad or mixed file."""
    records = read_runs(runs_path)
    report = build_report(
        records, golden, scan, seed=seed, resamples=resamples, generated_at=generated_at
    )
    json_path, md_path = write_report(report, out_dir or runs_path.parent)
    return report, json_path, md_path
