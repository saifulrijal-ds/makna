"""WP-15 acceptance 7 (what can be checked without Docker): the CI stack job runs the evals in the
app container, uploads the report even when a gate fails, and fails the job when one does. That
the job passes is proven by CI itself."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
STEPS: list[dict[str, Any]] = CI["jobs"]["stack"]["steps"]
DOCKERFILE = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def step(name_part: str) -> dict[str, Any]:
    (found,) = [s for s in STEPS if name_part in s.get("name", "")]
    return found


def test_the_stack_job_runs_all_evals_in_the_app_container() -> None:
    run = step("Retrieval and compile evals")["run"]
    assert "exec -T" in run and " app python -m evals all" in run
    assert "--out /tmp/eval-report" in run


def test_the_report_is_uploaded_whether_or_not_the_evals_passed() -> None:
    copy = step("Copy the eval report")
    assert copy["if"] == "always()" and "cp app:/tmp/eval-report/." in copy["run"]
    upload = step("Upload the eval report")
    assert upload["if"] == "always()"
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["path"] == "eval-report"
    assert STEPS.index(copy) < STEPS.index(upload)


def test_a_failed_gate_fails_the_job_after_the_other_checks_have_run() -> None:
    evals = step("Retrieval and compile evals")
    assert evals["continue-on-error"] is True and evals["id"] == "evals"
    last = STEPS[-1]
    assert last["if"] == "steps.evals.outcome == 'failure'"
    assert "exit 1" in last["run"]


def test_the_evals_run_after_the_engine_tests_and_before_the_stack_stops() -> None:
    names = [s.get("name", "") for s in STEPS]
    engine = next(i for i, n in enumerate(names) if n.startswith("Engine tests"))
    evals = next(i for i, n in enumerate(names) if n.startswith("Retrieval and compile evals"))
    stop = names.index("Stop the stack")
    assert engine < evals < stop


def test_the_engine_tests_get_a_scratch_database_server_for_the_compile_eval_tests() -> None:
    env = step("Engine tests")["env"]
    assert env["MAKNA_TEST_DATABASE_URL"].endswith("@127.0.0.1:5432/postgres")
    assert env["MAKNA_REQUIRE_TEST_DATABASE"] == "1"


def test_the_image_holds_the_eval_runner_and_the_golden_files() -> None:
    assert "COPY evals ./evals" in DOCKERFILE
    assert DOCKERFILE.index("COPY config ./config") < DOCKERFILE.index("COPY evals ./evals")
    assert "evals" not in DOCKERIGNORE  # the directory itself is needed in the image
    assert "evals/reports" in DOCKERIGNORE
