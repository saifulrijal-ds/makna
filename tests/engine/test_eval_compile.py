"""WP-15 acceptance 2 (second half) and 4: the compile eval on the sample database.

Part A, B and C pass on the sample data; breaking one expected CSV (in a temporary copy) makes the
compile gate fail and names the question; part B covers every approved metric and every available
non-personal dimension, counted independently from the index; engine error text stays out of the
report."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from evals import compile_eval, gates
from evals.cli import run_compile_eval
from evals.compare import Table
from evals.golden.reference import EXPECTED_DIR, QUESTIONS_FILE
from evals.golden.schema import load_questions
from evals.runtime import Runtime
from evals.sqlrun import SqlFailure, SqlRunner
from makna.model.request import CompileResponse, MetricRef, QueryRequest
from makna.services.snapshot import SnapshotCache

QUESTIONS = load_questions(QUESTIONS_FILE)


@pytest.fixture(scope="module")
def section(eval_runtime: Runtime) -> dict[str, Any]:
    return run_compile_eval(eval_runtime, QUESTIONS, verbose=False)


def test_every_part_passes_on_the_sample_data(section: dict[str, Any]) -> None:
    for part in ("part_a", "part_b", "part_c"):
        assert section[part]["failures"] == [], part
        assert section[part]["pass_rate"] == 1.0
    answers = [q for q in QUESTIONS if q.outcome == "answer"]
    candidates = sum(len(q.expected.candidates or ()) for q in QUESTIONS if q.outcome == "clarify")
    assert section["part_a"]["total"] == len(answers) + candidates
    refusals = [
        q
        for q in QUESTIONS
        if q.outcome in ("no_metric", "refuse") and q.expected.request is not None
    ]
    assert section["part_c"]["total"] == len(refusals) >= 4


def test_the_compile_gates_pass_and_the_exit_code_is_zero(section: dict[str, Any]) -> None:
    results = gates.evaluate({"compile": section}, gates.load_gates())
    assert len(results) == 3 and all(r.passed for r in results)
    assert gates.exit_code(results) == 0


def index_count(runtime: Runtime) -> int:
    """The number of part B requests, counted from the stored index: three kinds of request."""
    with runtime.engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT count(*) AS metrics,
                       count(*) FILTER (WHERE official ->> 'default_grain' IS NOT NULL) AS grains,
                       COALESCE(sum((
                         SELECT count(*) FROM jsonb_array_elements(derived -> 'dimensions') d
                         WHERE (d ->> 'available')::boolean AND d ->> 'sensitivity' <> 'personal'
                       )), 0) AS dimensions
                FROM semantic_object WHERE kind = 'metric' AND status = 'approved'
                """
            )
        ).one()
    return int(rows.metrics) + int(rows.grains) + int(rows.dimensions)


def test_part_b_executes_one_request_per_metric_grain_and_available_dimension(
    eval_runtime: Runtime, section: dict[str, Any]
) -> None:
    expected = index_count(eval_runtime)
    assert expected > 100  # 23 metrics and their dimensions: not a trivial count
    assert section["part_b"]["total"] == expected
    kinds = section["part_b"]["by_kind"]
    assert sum(kinds.values()) == expected and kinds["plain"] == 23

    # Count the statements actually sent to the engine, not the items reported.
    snapshot, _ = SnapshotCache(eval_runtime.engine).current()
    from makna.services import compile_on_snapshot

    def compile_fn(request: QueryRequest) -> CompileResponse:
        return compile_on_snapshot(
            snapshot, request, policy=eval_runtime.policy, dialect=eval_runtime.dialect
        )

    rows = compile_eval.approved_metric_rows(eval_runtime.engine)
    requests = compile_eval.part_b_requests(rows, eval_runtime.policy.fingerprint())
    assert len(requests) == expected
    assert eval_runtime.engine_url is not None
    with SqlRunner(eval_runtime.engine_url, 30) as runner:
        executed: list[str] = []

        class Counting:
            def run(self, sql: str) -> Table:
                executed.append(sql)
                return runner.run(sql)

        items = compile_eval.run_part_b(requests, compile_fn, Counting())
    assert len(executed) == expected
    assert [i for i in items if i.reason is not None] == []


def test_part_b_covers_every_approved_metric(
    eval_runtime: Runtime, section: dict[str, Any]
) -> None:
    with eval_runtime.engine.connect() as connection:
        approved = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM semantic_object WHERE kind = 'metric' AND status = 'approved'"
                )
            )
        }
    rows = compile_eval.approved_metric_rows(eval_runtime.engine)
    requests = compile_eval.part_b_requests(rows, eval_runtime.policy.fingerprint())
    assert {r.metric for r in requests} == approved
    assert len(approved) == 23


def test_a_broken_expected_csv_fails_the_compile_gate_and_names_the_question(
    eval_runtime: Runtime, tmp_path: Path
) -> None:
    copy = tmp_path / "expected"
    shutil.copytree(EXPECTED_DIR, copy)
    target = copy / "G02.csv"
    lines = target.read_text().splitlines()
    header, first = lines[0], lines[1]
    region, amount = first.split(",")
    lines[1] = f"{region},{float(amount) * 1.01:.2f}"
    target.write_text("\n".join([header, *lines[1:]]) + "\n")

    broken = run_compile_eval(eval_runtime, QUESTIONS, verbose=False, expected_dir=copy)
    assert broken["part_a"]["failures"] == [{"id": "G02", "reason": "mismatch:number"}]
    assert broken["part_b"]["failures"] == [] and broken["part_c"]["failures"] == []

    results = gates.evaluate({"compile": broken}, gates.load_gates())
    (failed,) = [r for r in results if not r.passed]
    assert failed.name == "compile.part_a_pass_rate_min"
    assert failed.failures == ("G02: mismatch:number",)
    assert gates.exit_code(results) == 1
    # The committed CSV is untouched.
    assert "G02" not in json.dumps(
        run_compile_eval(eval_runtime, QUESTIONS, verbose=False)["part_a"]["failures"]
    )


# ---- the engine's error text stays out of the report --------------------------------------------

PERSONAL = "Budi Santoso 3201010101010001"


def response_for(sql: str) -> CompileResponse:
    return CompileResponse(sql=sql, dialect="postgres", columns=(), metric=MetricRef(id="metric:m"))


def test_an_engine_error_is_reported_by_sqlstate_and_class_only(engine_url: str) -> None:
    sql = f"SELECT CAST('{PERSONAL}' AS integer)"
    logged: list[str] = []
    with SqlRunner(engine_url, 30) as runner:
        items = compile_eval.run_part_b(
            [compile_eval.PartBRequest("m", "plain", QueryRequest(metric="m"))],
            lambda request: response_for(sql),
            runner,
            log=logged.append,
        )
        with pytest.raises(SqlFailure) as caught:
            runner.run(sql)
    assert items[0].reason == "sql_error:22P02:InvalidTextRepresentation"
    summary = json.dumps(compile_eval.part_summary(items))
    assert PERSONAL not in summary and PERSONAL not in str(caught.value)
    # Only the log function, which the command passes for --verbose, receives the engine's text.
    assert len(logged) == 1 and PERSONAL in logged[0]


def test_the_connection_is_read_only(engine_url: str) -> None:
    with SqlRunner(engine_url, 30) as runner:
        with pytest.raises(SqlFailure) as caught:
            runner.run("CREATE TABLE eval_must_not_exist (x integer)")
        assert caught.value.sqlstate in ("25006", "42501")
        assert runner.run("SELECT 1 AS one").rows == ((1,),)
