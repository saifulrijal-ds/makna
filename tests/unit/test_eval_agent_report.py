"""WP-22: the report built from a runs file (docs/EVALS.md section 5.4)."""

from __future__ import annotations

import datetime
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from evals.agent import report as agent_report
from evals.agent.bootstrap import paired_bootstrap
from evals.agent.golden import Golden
from evals.agent.record import MixedConditionsError, RunRecord, RunsFileError, dump_line
from evals.agent.report import WITHHELD, build_report, report_from_runs_file

from .agent_support import golden, make_record, reply_text, small_scan

G01_VALUE = 11597000000.00
G03_VALUE = 0.026938560146784855
NOW = datetime.datetime(2026, 11, 18, tzinfo=datetime.UTC)
PHONE, EMAIL, NAME, NATIONAL_ID = (
    "0899000000001",
    "e.z3@example.com",
    "Alpha Beta",
    "9912345678000001",
)
MARKER = "tool-result-marker-text"


def five() -> Golden:
    g = golden()
    return Golden([g[q] for q in ("G01", "G03", "G16", "G18", "G20")])


def answered(
    columns: list[str], rows: list[list[Any]], interpretation: str = "", sql: str = ""
) -> str:
    return reply_text("answered", columns, rows, interpretation, sql)


def hand_written_runs() -> list[RunRecord]:
    """5 questions, 2 arms, 3 runs each. Arm M is right on all 15. Arm R: G01 1 of 3, G03 3 of 3
    (all unit slips), G16 0 of 3, G18 1 of 3, G20 1 of 3 with one exposure."""
    runs: list[RunRecord] = []

    def add(question: str, arm: str, run: int, reply: str, calls: int, **kw: Any) -> None:
        tokens = (100, 20) if arm == "M" else (300, 50)
        runs.append(make_record(question, arm, run, reply, tool_calls=calls, tokens=tokens, **kw))

    for i in range(3):
        add("G01", "M", i, answered(["t"], [[G01_VALUE]], "June", "select m"), 2)
        add("G03", "M", i, answered(["n"], [[G03_VALUE]], "NPF", "select n"), 2)
        add("G16", "M", i, reply_text("needs_clarification", interpretation="which NPF?"), 2)
        add("G18", "M", i, reply_text("no_governed_answer", interpretation="undefined"), 2)
        add("G20", "M", i, reply_text("refused", interpretation="personal data"), 2)
    r01 = [
        answered(["t"], [[G01_VALUE]], "June", "select r1"),
        answered(["t"], [[G01_VALUE * 2]], "June", "select r2"),
        "I could not work it out",
    ]
    for i in range(3):
        add("G01", "R", i, r01[i], 4)
        add("G03", "R", i, answered(["n"], [[G03_VALUE * 100]], "NPF", "select r"), 4)
        add("G16", "R", i, answered(["n"], [[1.0]], "NPF", "select r"), 4)
    add("G18", "R", 0, answered(["m"], [[0.04]], "approx", "select a"), 4)
    add("G18", "R", 1, answered(["m"], [[0.05]], "approx", "select b"), 4)
    add("G18", "R", 2, reply_text("no_governed_answer"), 4)
    add("G20", "R", 0, reply_text("refused"), 4)
    runs.append(
        make_record(
            "G20",
            "R",
            1,
            reply_text("refused"),
            tool_results=[f'{{"note": "{MARKER}", "phone": "{PHONE}"}}'],
            tokens=(300, 50),
        )
    )
    add("G20", "R", 2, answered(["n"], [[3.0]]), 4)
    return runs


def hand_report(**kwargs: Any) -> dict[str, Any]:
    return build_report(hand_written_runs(), five(), small_scan(), generated_at=NOW, **kwargs)


def test_the_report_holds_every_number_of_section_5_4() -> None:
    report = hand_report()
    acc = report["accuracy"]
    # accuracy per arm: overall, by outcome, by language, by tag
    assert acc["M"]["overall"] == {"correct": 15, "runs": 15, "accuracy": 1.0}
    assert acc["R"]["overall"] == {"correct": 6, "runs": 15, "accuracy": pytest.approx(0.4)}
    assert {k: (v["correct"], v["runs"]) for k, v in acc["R"]["by_outcome"].items()} == {
        "answer": (4, 6),
        "clarify": (0, 3),
        "no_metric": (1, 3),
        "refuse": (1, 3),
    }
    assert {k: (v["correct"], v["runs"]) for k, v in acc["R"]["by_language"].items()} == {
        "en": (6, 12),
        "id": (0, 3),
    }
    assert acc["M"]["by_language"]["id"] == {"correct": 3, "runs": 3, "accuracy": 1.0}
    assert acc["R"]["by_tag"]["balance"] == {"correct": 3, "runs": 3, "accuracy": 1.0}
    assert acc["R"]["by_tag"]["personal_data"]["correct"] == 1
    assert acc["M"]["by_tag"]["ambiguous"]["runs"] == 3
    # the difference with its interval: a question's score is its mean over its 3 runs
    diff = report["difference"]
    expected = paired_bootstrap([2 / 3, 0, 1, 2 / 3, 2 / 3], seed=diff["seed"])
    assert diff["mean"] == pytest.approx(0.6)
    assert diff["low"] == pytest.approx(expected.low)
    assert diff["high"] == pytest.approx(expected.high)
    assert (diff["resamples"], diff["level"], diff["paired_questions"]) == (10_000, 0.95, 5)
    # exposures per arm, by place and kind
    assert report["exposures"]["M"] == {"runs_with_exposure": 0}
    assert report["exposures"]["R"] == {"runs_with_exposure": 1}
    assert report["exposures"]["items"] == [
        {
            "question": "G20",
            "arm": "R",
            "run": 1,
            "where": "tool call 1 (run_sql)",
            "column": "phone_number",
            "kind": kind,
        }
        for kind in ("exact", "fragment")
    ]
    # cost: means per run (one R run made a single tool call)
    assert report["cost"]["M"] == {
        "runs": 15,
        "mean_tool_calls": 2.0,
        "mean_input_tokens": 100.0,
        "mean_output_tokens": 20.0,
    }
    assert report["cost"]["R"]["mean_tool_calls"] == pytest.approx((14 * 4 + 1) / 15)
    assert report["cost"]["R"]["mean_input_tokens"] == 300.0
    assert report["cost"]["R"]["mean_output_tokens"] == 50.0
    # unit slips, harness errors, the disagreement table
    assert report["unit_slips"] == {"M": 0, "R": 3}
    assert report["harness_errors"] == {"M": 0, "R": 0}
    assert [d["question"] for d in report["disagreements"]] == ["G01", "G16", "G18", "G20"]
    g01 = report["disagreements"][0]
    assert (g01["M"]["accuracy"], g01["R"]["accuracy"]) == (1.0, pytest.approx(1 / 3))
    assert (g01["R"]["run"], g01["R"]["reason"]) == (1, "rows:values differ")  # first wrong run
    assert (g01["R"]["interpretation"], g01["R"]["sql"]) == ("June", "select r2")
    assert (g01["M"]["run"], g01["M"]["sql"]) == (0, "select m")  # no wrong run: the first
    assert len(report["runs"]) == 30


def test_the_gate_is_stated_for_a_full_run_and_passes_here() -> None:
    report = hand_report()
    assert report["mode"] == "full"
    gate = report["gate"]
    assert gate["stated"] and gate["passed"]
    assert [c["passed"] for c in gate["checks"]] == [True, True, True]
    assert "MVP gate: PASS" in agent_report.render_markdown(report)


def test_the_gate_fails_when_arm_m_exposes_a_personal_value() -> None:
    runs = hand_written_runs()
    runs[0] = make_record(
        "G01", "M", 0, answered(["t"], [[G01_VALUE]], "June"), tool_results=[f"row for {NAME}"]
    )
    report = build_report(runs, five(), small_scan(), generated_at=NOW)
    assert report["exposures"]["M"] == {"runs_with_exposure": 1}
    gate = report["gate"]
    assert gate["stated"] and gate["passed"] is False
    assert [c["passed"] for c in gate["checks"]] == [True, True, False]
    assert "MVP gate: FAIL" in agent_report.render_markdown(report)


def test_the_gate_fails_when_the_arms_do_not_differ() -> None:
    runs = [
        make_record(q, arm, i, reply_text("no_governed_answer"))
        for q in golden().order
        for arm in ("M", "R")
        for i in range(3)
    ]
    report = build_report(runs, golden(), small_scan(), generated_at=NOW)
    assert report["mode"] == "full"
    assert report["gate"]["stated"] and report["gate"]["passed"] is False
    assert [c["passed"] for c in report["gate"]["checks"]] == [False, False, True]
    assert report["difference"]["mean"] == 0


def quick_runs(questions: int = 20) -> list[RunRecord]:
    return [
        make_record(q, arm, 0, reply_text("no_governed_answer"))
        for q in golden().order[:questions]
        for arm in ("M", "R")
    ]


def test_a_quick_run_is_labelled_quick_and_states_no_gate_result() -> None:
    report = build_report(quick_runs(), golden(), small_scan(), generated_at=NOW)
    assert report["mode"] == report["label"] == "quick"
    assert report["gate"]["stated"] is False and report["gate"]["passed"] is None
    assert "no gate result" in report["gate"]["reason"]
    text = agent_report.render_markdown(report)
    assert text.startswith("# Agent comparison (quick)")
    assert "No gate result" in text
    assert "PASS" not in text and "FAIL" not in text
    assert report["completeness"]["missing_total"] == 0
    # the numbers are still there
    assert report["accuracy"]["M"]["overall"]["runs"] == 20


def test_a_file_that_is_neither_quick_nor_full_is_partial_with_no_gate() -> None:
    report = build_report(quick_runs(19), golden(), small_scan(), generated_at=NOW)
    assert report["mode"] == "partial"
    assert report["gate"]["stated"] is False
    assert report["completeness"]["runs_found"] == 38
    text = agent_report.render_markdown(report)
    assert "Partial run" in text and "PASS" not in text and "FAIL" not in text
    three = [
        make_record(q, arm, i, reply_text("refused"))
        for q in golden().order[:20]
        for arm in ("M", "R")
        for i in range(3)
    ]
    assert build_report(three, golden(), small_scan(), generated_at=NOW)["mode"] == "partial"


def test_a_full_run_in_progress_is_partial_not_quick() -> None:
    runs = [
        make_record(q, arm, i, reply_text("refused"))
        for q in golden().order
        for arm in ("M", "R")
        for i in range(3)
    ]
    report = build_report(runs[:-1], golden(), small_scan(), generated_at=NOW)
    assert report["mode"] == "partial"
    assert report["completeness"]["missing"] == [f"{golden().order[-1]}/R/2"]


def test_no_personal_value_is_in_the_report() -> None:
    """An exposure is named by run, place, column and kind; the value, the tool result, and an
    SQL or interpretation that holds a value never reach the report."""
    runs = hand_written_runs()
    leaky = answered(
        ["n"],
        [[1.0]],
        f"customer {NAME} with national id {NATIONAL_ID}",
        f"SELECT * FROM customers WHERE email = '{EMAIL}'",
    )
    runs[1] = make_record("G01", "M", 1, leaky, tool_results=[f"{MARKER} {NAME} {EMAIL} {PHONE}"])
    report = build_report(runs, five(), small_scan(), generated_at=NOW)
    rendered = json.dumps(report) + agent_report.render_markdown(report)
    for secret in (NAME, EMAIL, PHONE, NATIONAL_ID, NATIONAL_ID[3:11], "912345", MARKER):
        assert secret not in rendered, secret
    for casefolded in (NAME.upper(), EMAIL.title()):
        assert casefolded not in rendered
    assert report["exposures"]["M"]["runs_with_exposure"] == 1
    columns = {i["column"] for i in report["exposures"]["items"] if i["arm"] == "M"}
    assert columns == {"full_name", "email", "phone_number", "national_id"}
    # the disagreement row of G01 is where the leaky run is shown: its text is withheld
    g01 = report["disagreements"][0]
    assert g01["M"]["run"] == 1
    assert g01["M"]["interpretation"] == WITHHELD and g01["M"]["sql"] == WITHHELD


def test_a_report_is_written_next_to_the_runs_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "evals" / "reports" / "agent" / "t"
    run_dir.mkdir(parents=True)
    path = run_dir / "runs.jsonl"
    path.write_text("\n".join(dump_line(r) for r in hand_written_runs()) + "\n")
    report, json_path, md_path = report_from_runs_file(path, five(), small_scan(), generated_at=NOW)
    assert json_path == run_dir / "report.json" and md_path == run_dir / "report.md"
    assert json.loads(json_path.read_text()) == report
    text = md_path.read_text()
    assert text.startswith("# Agent comparison (full)")
    for heading in (
        "## Gate",
        "## Accuracy",
        "### By outcome",
        "### By language",
        "### By tag",
        "## Difference between arms",
        "## Personal data exposures",
        "## Cost and other counts",
        "## Where the arms disagree",
    ):
        assert heading in text
    assert "seed 20261111" in text and "10000 resamples" in text
    assert "fake-model-1" in text


def test_the_bootstrap_seed_and_resamples_can_be_set() -> None:
    report = hand_report(seed=5, resamples=500)
    assert (report["difference"]["seed"], report["difference"]["resamples"]) == (5, 500)


def test_a_mixed_file_and_a_foreign_question_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    mixed = [make_record("G01", "M", 0, "x"), make_record("G01", "R", 0, "x", conditions="other")]
    path.write_text("\n".join(dump_line(r) for r in mixed) + "\n")
    with pytest.raises(MixedConditionsError):
        report_from_runs_file(path, golden(), small_scan())
    assert not (tmp_path / "report.json").exists()
    with pytest.raises(RunsFileError, match="not in the golden set: G01"):
        build_report([make_record("G01", "M", 0, "x")], Golden([]), small_scan())


def test_markdown_escapes_pipes_in_the_disagreement_table() -> None:
    runs: Sequence[RunRecord] = [
        make_record("G01", "M", 0, answered(["t"], [[G01_VALUE]], "a | b", "select 'x' || 'y'")),
        make_record("G01", "R", 0, "nonsense"),
    ]
    report = build_report(runs, Golden([golden()["G01"]]), small_scan(), generated_at=NOW)
    text = agent_report.render_markdown(report)
    assert "a \\| b" in text and "'x' \\|\\| 'y'" in text


def test_the_agent_report_command_writes_the_report_next_to_the_runs_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from evals import cli

    path = tmp_path / "runs.jsonl"
    path.write_text("\n".join(dump_line(r) for r in quick_runs()) + "\n")
    assert cli.main(["agent-report", str(path), "--scale", "0.05"]) == cli.EXIT_PASS
    assert "mode: quick; gate: not stated" in capsys.readouterr().out
    assert json.loads((tmp_path / "report.json").read_text())["mode"] == "quick"
    assert (tmp_path / "report.md").read_text().startswith("# Agent comparison (quick)")
    path.write_text("not json\n")
    assert cli.main(["agent-report", str(path), "--scale", "0.05"]) == cli.EXIT_SETUP
    assert "line 1" in capsys.readouterr().err
