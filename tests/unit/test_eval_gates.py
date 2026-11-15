"""WP-15 acceptance 2 (first half): the exit code is 1 when a gate fails."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from evals import gates
from evals.gates import GateError, evaluate, exit_code, load_gates

from .eval_support import compile_section, full_report, lowered, retrieval_section


@pytest.fixture(scope="module")
def committed() -> dict[str, dict[str, float]]:
    return load_gates()


def test_the_committed_gates_are_the_thresholds_of_the_eval_design(
    committed: dict[str, dict[str, float]],
) -> None:
    assert committed["retrieval"] == {
        "recall_at_3_min": 0.90,
        "mrr_min": 0.75,
        "false_abstention_rate_max": 0.05,
        "indonesian_recall_gap_max": 0.10,
        "latency_p95_ms_below": 500,
    }
    assert committed["compile"] == {
        "part_a_pass_rate_min": 1.0,
        "part_b_pass_rate_min": 1.0,
        "part_c_pass_rate_min": 1.0,
    }


def test_a_good_report_passes_every_gate_and_exits_zero(
    committed: dict[str, dict[str, float]],
) -> None:
    results = evaluate(full_report(), committed)
    assert len(results) == len(gates.SPECS) == 8
    assert all(r.passed for r in results)
    assert exit_code(results) == 0


def test_a_lowered_gate_fixture_fails_and_the_exit_code_is_one(
    committed: dict[str, dict[str, float]],
) -> None:
    # The report is fine for the committed gates; a fixture that demands more than it can give
    # fails, and the failing gate is named.
    strict = lowered(committed, "retrieval", "latency_p95_ms_below", 5)
    results = evaluate(full_report(), strict)
    failed = [r.name for r in results if not r.passed]
    assert failed == ["retrieval.latency_p95_ms_below"]
    assert exit_code(results) == 1


def test_recall_below_its_gate_fails(committed: dict[str, dict[str, float]]) -> None:
    results = evaluate(full_report(retrieval={"recall_ok": False}), committed)
    failed = {r.name for r in results if not r.passed}
    assert "retrieval.recall_at_3_min" in failed
    assert "retrieval.indonesian_recall_gap_max" in failed  # English 1.0, Indonesian 0.0
    assert exit_code(results) == 1


def test_a_gate_at_the_threshold_passes(committed: dict[str, dict[str, float]]) -> None:
    exact = lowered(committed, "retrieval", "recall_at_3_min", 1.0)
    assert all(r.passed for r in evaluate(full_report(), exact))
    # The latency gate is strict: equal to the threshold fails.
    section = full_report(retrieval={"latency": [499.0, 500.0]})
    results = evaluate(section, committed)
    assert not next(r for r in results if r.name.endswith("latency_p95_ms_below")).passed


def test_a_failing_compile_part_names_each_failing_request(
    committed: dict[str, dict[str, float]],
) -> None:
    report = full_report(compile={"failing": {"part_a": ("G07", "mismatch:number")}})
    results = evaluate(report, committed)
    (failed,) = [r for r in results if not r.passed]
    assert failed.name == "compile.part_a_pass_rate_min"
    assert failed.failures == ("G07: mismatch:number",)
    assert exit_code(results) == 1


def test_a_gate_that_cannot_be_computed_fails(committed: dict[str, dict[str, float]]) -> None:
    report = {
        "compile": {
            **compile_section(),
            "part_c": {"total": 0, "passed": 0, "pass_rate": None, "failures": [], "items": []},
        }
    }
    results = evaluate(report, committed)
    assert [r.name for r in results if not r.passed] == ["compile.part_c_pass_rate_min"]


def test_only_the_sections_that_ran_are_evaluated(committed: dict[str, dict[str, float]]) -> None:
    only_retrieval = evaluate({"retrieval": retrieval_section()}, committed)
    assert {r.name.split(".")[0] for r in only_retrieval} == {"retrieval"}
    only_compile = evaluate({"compile": compile_section()}, committed)
    assert {r.name.split(".")[0] for r in only_compile} == {"compile"}


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "gates.toml"
    path.write_text(text)
    return path


def test_a_missing_gate_is_an_error_so_a_deleted_line_cannot_switch_one_off(
    tmp_path: Path,
) -> None:
    text = gates.GATES_FILE.read_text().replace("mrr_min = 0.75\n", "")
    with pytest.raises(GateError, match="missing mrr_min"):
        load_gates(write(tmp_path, text))


def test_an_unknown_gate_is_an_error(tmp_path: Path) -> None:
    text = gates.GATES_FILE.read_text() + "\n[retrieval_extra]\nx = 1\n"
    with pytest.raises(GateError, match="unknown section"):
        load_gates(write(tmp_path, text))
    text = gates.GATES_FILE.read_text().replace("[compile]\n", "[compile]\nrecall_typo = 0.5\n")
    with pytest.raises(GateError, match="unknown gate recall_typo"):
        load_gates(write(tmp_path, text))


def test_a_threshold_must_be_a_number(tmp_path: Path) -> None:
    text = gates.GATES_FILE.read_text().replace("mrr_min = 0.75", 'mrr_min = "0.75"')
    with pytest.raises(GateError, match="must be a number"):
        load_gates(write(tmp_path, text))


def test_the_gates_file_is_valid_toml() -> None:
    assert tomllib.loads(gates.GATES_FILE.read_text())
