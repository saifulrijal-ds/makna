"""The compile correctness eval (EVL-3, docs/EVALS.md section 4).

Part A compiles the request of every `answer` question and every candidate of a `clarify`
question, runs the SQL on the sample database, and compares it with the expected CSV. Part B
compiles and runs every approved metric: with no dimension, at its default grain, and by each
available non-personal dimension (as the indexer stored them under the running policy). Part C
checks that the requests of the refusal questions fail with the expected error code.

The compile path is `makna.services.compile_on_snapshot`, the function `compile_query` calls, on
the approved snapshot read from the database, so an eval compiles what an agent token gets. Each
part is a list of items; an item passes or fails with a short reason (a code, never a value or an
engine message). A part's score is the share of its items that pass.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine, text

from evals.compare import Table, compare_results, read_expected_csv
from evals.golden.reference import EXPECTED_DIR
from evals.golden.schema import ERROR_CODE_FOR_REASON, GoldenRequest, Question
from evals.sqlrun import SqlFailure
from makna.model.request import CompileResponse, QueryError, QueryRequest
from makna.services.errors import ServiceError

FULL_RANGE = ("2026-01-01", "2026-07-01")
"""Part B, requests 1 and 2: the first half of 2026."""
LATEST_MONTH = ("2026-06-01", "2026-07-01")
"""Part B, request 3: the latest month of the data."""

CURRENCY_UNIT = re.compile(r"^[A-Z]{3}$")

REFUSAL_CODES = {
    **ERROR_CODE_FOR_REASON,
    "personal_data": "PERSONAL_DATA_REFUSED",
}
"""The error code a refusal request must fail with, by the question's reason. One reading differs
from `ERROR_CODE_FOR_REASON`: `not_approved` is `NOT_APPROVED` for a curator, but the evals compile
as an agent token, who sees the approved snapshot only and gets `UNKNOWN_METRIC` for a draft (the
same answer as for an id that does not exist, ARCHITECTURE 7.7). Part C therefore expects
`UNKNOWN_METRIC` there and checks in the index that the metric really is a draft."""
TOKEN_CODES = {"not_approved": "UNKNOWN_METRIC"}
UNAPPROVED_STATUSES = ("draft", "in_review")

CompileFn = Callable[[QueryRequest], CompileResponse]


class Runner(Protocol):
    def run(self, sql: str) -> Table: ...


@dataclass(frozen=True)
class Item:
    id: str
    reason: str | None
    """None when the item passed; else a short code-like reason."""
    rows: int | None = None


@dataclass(frozen=True)
class PartBRequest:
    metric: str
    kind: str
    """`plain`, `default_grain`, or `dimension`."""
    request: QueryRequest
    dimension: str | None = None

    @property
    def id(self) -> str:
        suffix = {"plain": "", "default_grain": " @grain", "dimension": f" by {self.dimension}"}
        return f"{self.metric}{suffix[self.kind]}"


def part_summary(items: Sequence[Item]) -> dict[str, Any]:
    passed = sum(1 for item in items if item.reason is None)
    return {
        "total": len(items),
        "passed": passed,
        "pass_rate": passed / len(items) if items else None,
        "failures": [{"id": i.id, "reason": i.reason} for i in items if i.reason is not None],
        "items": [
            {"id": i.id, "ok": i.reason is None, "reason": i.reason, "rows": i.rows} for i in items
        ],
    }


def _error_code(error: QueryError | ServiceError) -> str:
    return str(error.code)


def _execute(
    request: QueryRequest,
    compile_fn: CompileFn,
    runner: Runner,
    log: Callable[[str], None],
    label: str,
) -> tuple[CompileResponse, Table] | Item:
    """Compile and run one request, or the failing `Item`."""
    try:
        response = compile_fn(request)
    except (QueryError, ServiceError) as error:
        return Item(label, f"compile_error:{_error_code(error)}")
    try:
        return response, runner.run(response.sql)
    except SqlFailure as failure:
        log(f"{label}: {failure.message}")
        return Item(label, f"sql_error:{failure.sqlstate or 'unknown'}:{failure.error_class}")


def _roles(response: CompileResponse, answer_columns: Sequence[str]) -> tuple[set[str], set[str]]:
    keys = {c.name for c in response.columns if c.role in ("time", "dimension")}
    money = {c.name for c in response.columns if c.unit and CURRENCY_UNIT.match(c.unit)}
    return keys & set(answer_columns), money & set(answer_columns)


def run_part_a(
    questions: Sequence[Question],
    compile_fn: CompileFn,
    runner: Runner,
    *,
    expected_dir: Path = EXPECTED_DIR,
    log: Callable[[str], None] = lambda message: None,
) -> list[Item]:
    """Golden requests: compile, run, compare with the expected CSV. `log` receives text that may
    hold values or the engine's message; the caller passes it only for `--verbose`."""
    targets: list[tuple[str, str, GoldenRequest, list[str]]] = []
    for question in questions:
        expected = question.expected
        if question.outcome == "answer":
            assert expected.request is not None and expected.answer_columns is not None
            targets.append((question.id, question.id, expected.request, expected.answer_columns))
        elif question.outcome == "clarify":
            for candidate in expected.candidates or ():
                label = f"{question.id}/{candidate.metric}"
                stem = f"{question.id}_{candidate.metric}"
                targets.append((label, stem, candidate.request, candidate.answer_columns))
    items: list[Item] = []
    for label, stem, golden, answer_columns in targets:
        csv_path = expected_dir / f"{stem}.csv"
        if not csv_path.exists():
            items.append(Item(label, "no_expected_file"))
            continue
        outcome = _execute(golden.to_query_request(), compile_fn, runner, log, label)
        if isinstance(outcome, Item):
            items.append(outcome)
            continue
        response, got = outcome
        keys, money = _roles(response, answer_columns)
        difference = compare_results(
            read_expected_csv(csv_path),
            got,
            answer_columns,
            key_columns=keys,
            currency_columns=money,
        )
        if difference is None:
            items.append(Item(label, None, rows=len(got.rows)))
        else:
            log(f"{label}: {difference.describe(verbose=True)}")
            items.append(Item(label, f"mismatch:{difference.kind}", rows=len(got.rows)))
    return items


# --- part B ----------------------------------------------------------------------------------


def part_b_requests(rows: Sequence[Mapping[str, Any]], fingerprint: str) -> list[PartBRequest]:
    """The requests of part B from the approved metrics as the index stores them.

    `rows` are the `semantic_object` rows of approved metrics, in id order: `name`, `official`
    (for `default_grain`), and `derived` (the available dimensions computed under the policy
    whose fingerprint is `fingerprint`). Raises `ValueError` when the stored dimensions were
    computed under another policy, since part B would then test dimensions the running server
    does not offer.
    """
    requests: list[PartBRequest] = []
    for row in rows:
        name = row["name"]
        derived = row["derived"] or {}
        basis = derived.get("basis") or {}
        if "dimensions" not in derived or basis.get("governance_fingerprint") != fingerprint:
            raise ValueError(
                f"metric {name}: the index holds no available dimensions for the running "
                "governance policy; run `makna index build`"
            )
        start, end = FULL_RANGE
        requests.append(
            PartBRequest(name, "plain", QueryRequest(metric=name, time_start=start, time_end=end))
        )
        grain = (row["official"] or {}).get("default_grain")
        if grain:
            requests.append(
                PartBRequest(
                    name,
                    "default_grain",
                    QueryRequest(metric=name, grain=grain, time_start=start, time_end=end),
                )
            )
        month_start, month_end = LATEST_MONTH
        for option in derived["dimensions"]:
            if option.get("available") and option.get("sensitivity") != "personal":
                ref = option["ref"]
                requests.append(
                    PartBRequest(
                        name,
                        "dimension",
                        QueryRequest(
                            metric=name,
                            dimensions=(ref,),
                            time_start=month_start,
                            time_end=month_end,
                        ),
                        dimension=ref,
                    )
                )
    return requests


def approved_metric_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT name, official, derived FROM semantic_object "
                "WHERE kind = 'metric' AND status = 'approved' ORDER BY id"
            )
        )
        return [dict(row._mapping) for row in rows]


def run_part_b(
    requests: Sequence[PartBRequest],
    compile_fn: CompileFn,
    runner: Runner,
    *,
    log: Callable[[str], None] = lambda message: None,
) -> list[Item]:
    """Each request must compile, run without error, and return at least one row."""
    items: list[Item] = []
    for entry in requests:
        outcome = _execute(entry.request, compile_fn, runner, log, entry.id)
        if isinstance(outcome, Item):
            items.append(outcome)
            continue
        _, got = outcome
        items.append(Item(entry.id, None if got.rows else "no_rows", rows=len(got.rows)))
    return items


# --- part C ----------------------------------------------------------------------------------


def run_part_c(
    questions: Sequence[Question],
    compile_fn: CompileFn,
    status_of: Callable[[str], str | None],
) -> list[Item]:
    """The requests of `no_metric` questions (reasons `dimension_not_available` and
    `not_approved`) and of `refuse` questions that name one must fail with the expected code."""
    items: list[Item] = []
    for question in questions:
        expected = question.expected
        if question.outcome not in ("no_metric", "refuse") or expected.request is None:
            continue
        reason = expected.reason or ""
        code = TOKEN_CODES.get(reason) or REFUSAL_CODES[reason]
        request = expected.request.to_query_request()
        try:
            compile_fn(request)
        except (QueryError, ServiceError) as error:
            got = _error_code(error)
            if got != code:
                items.append(Item(question.id, f"wrong_error:{got}:expected:{code}"))
            elif reason == "not_approved" and status_of(request.metric) not in UNAPPROVED_STATUSES:
                items.append(Item(question.id, "metric_is_not_a_draft"))
            else:
                items.append(Item(question.id, None))
        else:
            items.append(Item(question.id, f"no_error:expected:{code}"))
    return items


def metric_status_reader(engine: Engine) -> Callable[[str], str | None]:
    def status_of(metric: str) -> str | None:
        with engine.connect() as connection:
            value = connection.execute(
                text("SELECT status FROM semantic_object WHERE id = :id"),
                {"id": f"metric:{metric}"},
            ).scalar_one_or_none()
        return None if value is None else str(value)

    return status_of


def stderr_log(message: str) -> None:
    print(message, file=sys.stderr)
