"""The `makna-eval` command: `uv run python -m evals <command>`.

    all         retrieval and compile; writes report.md and report.json; exit 1 if a gate fails
    retrieval   the retrieval eval only (docs/EVALS.md section 3)
    compile     the compile correctness eval only (section 4)
    tune        grid over the weights on the tuned split; prints the setting the rule chooses
    freeze      re-run the reference SQL and rewrite evals/golden/expected/ (after a deliberate
                change of the data or of a definition)
    agent-report  build the agent comparison report from a runs file (docs/EVALS.md section 5;
                no model, no database; WP-22B adds `agent`, which records the runs)

Exit codes: 0 every gate passed, 1 a gate failed (each failing gate, question, or request is
named), 2 the evals could not run (a setting, database, or model problem).

The agent comparison (`agent`, docs/EVALS.md section 5) is added to `COMMANDS` by WP-22 and
WP-22B. It is not part of `all`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy.exc import SQLAlchemyError

from evals import compile_eval, gates, heldout_eval, report, retrieval, tune
from evals.agent.bootstrap import DEFAULT_SEED as DEFAULT_BOOTSTRAP_SEED
from evals.agent.golden import load_golden
from evals.agent.record import RunsFileError
from evals.agent.report import report_from_runs_file
from evals.agent.scan import generated_scan
from evals.golden import freeze as freeze_module
from evals.golden.heldout import HeldoutFile, load_heldout_questions
from evals.golden.reference import EXPECTED_DIR, QUESTIONS_FILE, SEARCH_QUESTIONS_FILE
from evals.golden.schema import Question, SearchQuestion, load_questions, load_search_questions
from evals.runtime import (
    EvalSetupError,
    Runtime,
    describe_settings,
    open_runtime,
    require_engine_url,
)
from evals.sqlrun import SqlFailure, SqlRunner
from makna.search.embedding import EmbeddingError
from makna.search.hybrid import SearchError
from makna.services import compile_on_snapshot, require_catalog
from makna.services.errors import ServiceError
from makna.services.snapshot import SnapshotCache

EXIT_PASS, EXIT_GATE, EXIT_SETUP = 0, 1, 2


def run_retrieval_eval(
    runtime: Runtime,
    questions: Sequence[Question],
    search_questions: Sequence[SearchQuestion],
    heldout: HeldoutFile,
) -> dict[str, Any]:
    searchers = retrieval.make_searchers(runtime.engine, runtime.provider, runtime.search)
    section = retrieval.run_retrieval(
        runtime.engine, searchers, questions, search_questions=search_questions
    )
    section["heldout"] = heldout_eval.run_heldout(runtime.engine, searchers, heldout)
    return section


def log_for(verbose: bool) -> Callable[[str], None]:
    """Where engine error text and differing values go: standard error with `--verbose`, else
    nowhere."""
    return compile_eval.stderr_log if verbose else lambda message: None


def run_compile_eval(
    runtime: Runtime,
    questions: Sequence[Question],
    *,
    verbose: bool,
    expected_dir: Path = EXPECTED_DIR,
) -> dict[str, Any]:
    cache = SnapshotCache(runtime.engine)
    snapshot, _ = cache.current()
    require_catalog(snapshot)

    def compile_fn(request: Any) -> Any:
        return compile_on_snapshot(
            snapshot, request, policy=runtime.policy, dialect=runtime.dialect
        )

    log = log_for(verbose)
    rows = compile_eval.approved_metric_rows(runtime.engine)
    try:
        requests = compile_eval.part_b_requests(rows, runtime.policy.fingerprint())
    except ValueError as error:
        raise EvalSetupError(str(error)) from None
    url = require_engine_url(runtime)
    try:
        with SqlRunner(url, runtime.statement_timeout_seconds) as runner:
            part_a = compile_eval.run_part_a(
                questions, compile_fn, runner, expected_dir=expected_dir, log=log
            )
            part_b = compile_eval.run_part_b(requests, compile_fn, runner, log=log)
    except SqlFailure as failure:
        if verbose:
            print(failure.message, file=sys.stderr)
        raise EvalSetupError(f"cannot use the engine database: {failure}") from None
    part_c = compile_eval.run_part_c(
        questions, compile_fn, compile_eval.metric_status_reader(runtime.engine)
    )
    return {
        "part_a": compile_eval.part_summary(part_a),
        "part_b": {
            **compile_eval.part_summary(part_b),
            "by_kind": {
                kind: sum(1 for r in requests if r.kind == kind)
                for kind in ("plain", "default_grain", "dimension")
            },
        },
        "part_c": compile_eval.part_summary(part_c),
        "part_b_without_default_grain": [
            row["name"] for row in rows if not (row["official"] or {}).get("default_grain")
        ],
    }


def evaluate_command(args: argparse.Namespace) -> int:
    """`all`, `retrieval`, and `compile`: run, score against the gates, write the report."""
    try:
        thresholds = gates.load_gates(Path(args.gates) if args.gates else gates.GATES_FILE)
        questions = load_questions(QUESTIONS_FILE)
        search_questions = load_search_questions(SEARCH_QUESTIONS_FILE)
        heldout = load_heldout_questions()
    except (gates.GateError, ValueError) as error:
        print(f"makna-eval: {error}", file=sys.stderr)
        return EXIT_SETUP
    runtime: Runtime | None = None
    try:
        runtime = open_runtime()
        retrieval_section = compile_section = None
        if args.command in ("all", "retrieval"):
            print("makna-eval: retrieval ...", flush=True)
            retrieval_section = run_retrieval_eval(runtime, questions, search_questions, heldout)
        if args.command in ("all", "compile"):
            print("makna-eval: compile ...", flush=True)
            compile_section = run_compile_eval(runtime, questions, verbose=args.verbose)
        settings = describe_settings(runtime)
    except (EvalSetupError, SearchError, EmbeddingError, ServiceError) as error:
        print(f"makna-eval: cannot run: {error}", file=sys.stderr)
        return EXIT_SETUP
    except (SQLAlchemyError, psycopg.Error) as error:
        # Driver text can carry the connection parameters
        # (docs/notes/database-error-text-carries-the-query-parameters.md): the class only.
        print(
            f"makna-eval: cannot run: the database failed ({type(error).__name__}); "
            "check MAKNA_DATABASE_URL and that the database is up",
            file=sys.stderr,
        )
        return EXIT_SETUP
    finally:
        if runtime is not None:
            runtime.dispose()
    partial: dict[str, Any] = {}
    if retrieval_section is not None:
        partial["retrieval"] = retrieval_section
    if compile_section is not None:
        partial["compile"] = compile_section
    results = gates.evaluate(partial, thresholds)
    document = report.build_report(
        args.command,
        settings,
        results,
        retrieval=retrieval_section,
        compile_section=compile_section,
    )
    out_dir = Path(args.out) if args.out else report.default_out_dir()
    json_path, md_path = report.write_report(document, out_dir)
    print_summary(results)
    print(f"makna-eval: wrote {md_path} and {json_path}")
    return gates.exit_code(results)


def print_summary(results: Sequence[gates.GateResult]) -> None:
    for result in results:
        value = "n/a" if result.value is None else f"{result.value:.3f}"
        status = "pass" if result.passed else "FAIL"
        print(f"  {status:4}  {result.label}: {value} (required {result.op} {result.threshold:g})")
        for entry in result.failures:
            print(f"          {entry}")


def tune_command(args: argparse.Namespace) -> int:
    """`tune`: the weight, glossary term weight, and `low` grids over the tuned split only; prints
    the setting the rule of `evals/tune.py` chooses and writes `tuning.md` and `tuning.json`."""
    try:
        thresholds = gates.load_gates()["retrieval"]
        questions, search_questions = tune.load_tuned_questions()
    except (gates.GateError, ValueError) as error:
        print(f"makna-eval: {error}", file=sys.stderr)
        return EXIT_SETUP
    runtime: Runtime | None = None
    try:
        runtime = open_runtime()
        tuner = tune.Tuner(
            runtime.engine,
            tune.QueryCache(runtime.provider),
            runtime.search,
            questions,
            search_questions,
            thresholds,
        )
        result = tune.tune(tuner, log=lambda message: print(f"makna-eval: {message}", flush=True))
    except (EvalSetupError, SearchError, EmbeddingError, ServiceError, RuntimeError) as error:
        print(f"makna-eval: cannot run: {error}", file=sys.stderr)
        return EXIT_SETUP
    except (SQLAlchemyError, psycopg.Error) as error:
        print(
            f"makna-eval: cannot run: the database failed ({type(error).__name__}); "
            "check MAKNA_DATABASE_URL and that the database is up",
            file=sys.stderr,
        )
        return EXIT_SETUP
    finally:
        if runtime is not None:
            runtime.dispose()
    out_dir = Path(args.out) if args.out else report.default_out_dir()
    _, md_path = tune.write_result(result, out_dir)
    chosen = result["chosen"]
    print(
        f"chosen: w_keyword {chosen['w_keyword']}, w_vector {chosen['w_vector']:g}, "
        f"glossary_term weight {chosen['glossary_term_weight']}, low {chosen['low']}"
    )
    print(f"makna-eval: wrote {md_path}")
    return EXIT_PASS


def freeze_command(args: argparse.Namespace) -> int:
    del args
    return freeze_module.main([])


def agent_report_command(args: argparse.Namespace) -> int:
    """Score a runs file and write `report.json` and `report.md` (next to it by default)."""
    from data.generator import DEFAULT_SEED  # `data/` is not needed by the other commands

    runs = Path(args.runs)
    try:
        golden = load_golden()
        scan = generated_scan(DEFAULT_SEED if args.seed is None else args.seed, args.scale)
        report, _, md_path = report_from_runs_file(
            runs,
            golden,
            scan,
            out_dir=Path(args.out) if args.out else None,
            seed=args.bootstrap_seed,
        )
    except (RunsFileError, ValueError, OSError) as error:
        print(f"makna-eval: cannot report: {error}", file=sys.stderr)
        return EXIT_SETUP
    gate = report["gate"]
    state = "not stated" if not gate["stated"] else ("pass" if gate["passed"] else "fail")
    print(f"mode: {report['mode']}; gate: {state}")
    print(f"makna-eval: wrote {md_path}")
    return EXIT_PASS


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "all": evaluate_command,
    "retrieval": evaluate_command,
    "compile": evaluate_command,
    "tune": tune_command,
    "freeze": freeze_command,
    "agent-report": agent_report_command,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="makna-eval", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for name, text in (
        ("all", "retrieval and compile evals, with the gates"),
        ("retrieval", "the retrieval eval only"),
        ("compile", "the compile correctness eval only"),
    ):
        command = commands.add_parser(name, help=text, description=text)
        command.add_argument("--out", help="report directory (default: evals/reports/<date>)")
        command.add_argument("--gates", help="gates file (default: evals/gates.toml)")
        command.add_argument(
            "--verbose",
            action="store_true",
            help="print engine error text and differing values to standard error "
            "(they can hold personal data: never in a shared log)",
        )
    tuning = commands.add_parser(
        "tune",
        help="grid over the search weights on the tuned split only (never the held-out one)",
        description="Choose the search weights by the rule of evals/tune.py, reading only the "
        "golden and search questions. Prints the setting; changes no file but the report.",
    )
    tuning.add_argument("--out", help="report directory (default: evals/reports/<date>)")
    scoring = commands.add_parser(
        "agent-report",
        help="build the agent comparison report from a runs file",
        description="Score a runs file (docs/EVALS.md section 5) and write report.json and "
        "report.md next to it. Calls no model and needs no database; the personal data scan "
        "generates the sample customers again from the seed and scale.",
    )
    scoring.add_argument("runs", help="the runs.jsonl of a run")
    scoring.add_argument("--out", help="report directory (default: the runs file's directory)")
    scoring.add_argument(
        "--seed",
        type=int,
        help="seed the sample data was generated with (default: the data generator's)",
    )
    scoring.add_argument(
        "--scale", type=float, default=1.0, help="scale the sample data was generated with"
    )
    scoring.add_argument(
        "--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED, help="seed of the bootstrap"
    )
    commands.add_parser(
        "freeze",
        help="rewrite evals/golden/expected/ from the reference SQL",
        description="Rewrite the expected CSVs from the reference SQL (needs MAKNA_ENGINE_URL).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)
