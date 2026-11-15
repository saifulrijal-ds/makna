"""WP-15: the three compile parts, with a fake compiler and a fake engine (no database)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evals import compile_eval
from evals.compare import Table
from evals.compile_eval import Item, PartBRequest, part_b_requests, part_summary
from evals.golden.reference import EXPECTED_DIR, QUESTIONS_FILE
from evals.golden.schema import load_questions
from evals.sqlrun import SqlFailure, runnable_sql
from makna.model.request import (
    CompileResponse,
    ErrorCode,
    MetricRef,
    QueryError,
    QueryRequest,
    ResultColumn,
)
from makna.services.errors import ServiceError, ServiceErrorCode

QUESTIONS = load_questions(QUESTIONS_FILE)
BY_ID = {q.id: q for q in QUESTIONS}


def response(sql: str = "SELECT 1", columns: tuple[ResultColumn, ...] = ()) -> CompileResponse:
    return CompileResponse(
        sql=sql, dialect="postgres", columns=columns, metric=MetricRef(id="metric:m")
    )


class FakeRunner:
    def __init__(self, table: Table | Exception) -> None:
        self.table = table
        self.sql: list[str] = []

    def run(self, sql: str) -> Table:
        self.sql.append(sql)
        if isinstance(self.table, Exception):
            raise self.table
        return self.table


def expected_table(question_id: str) -> Table:
    from evals.compare import read_expected_csv

    return read_expected_csv(EXPECTED_DIR / f"{question_id}.csv")


def columns_of(question_id: str) -> tuple[ResultColumn, ...]:
    expected = BY_ID[question_id].expected
    assert expected.answer_columns is not None
    return tuple(
        ResultColumn(name=name, role="dimension" if "__" in name else "metric")
        for name in expected.answer_columns
    )


# ---- part A ------


def test_part_a_passes_when_the_result_equals_the_csv() -> None:
    table = expected_table("G02")
    typed = Table(table.columns, tuple((r[0], float(str(r[1]))) for r in table.rows))
    items = compile_eval.run_part_a(
        [BY_ID["G02"]],
        lambda request: response(columns=columns_of("G02")),
        FakeRunner(typed),
    )
    assert [(i.id, i.reason) for i in items] == [("G02", None)]


def test_part_a_names_the_question_when_a_value_differs() -> None:
    table = expected_table("G02")
    wrong = Table(table.columns, tuple((r[0], 1.0) for r in table.rows))
    (item,) = compile_eval.run_part_a(
        [BY_ID["G02"]], lambda request: response(columns=columns_of("G02")), FakeRunner(wrong)
    )
    assert (item.id, item.reason) == ("G02", "mismatch:number")


def test_part_a_covers_each_candidate_of_a_clarify_question() -> None:
    seen: list[str] = []

    def compile_fn(request: QueryRequest) -> CompileResponse:
        seen.append(request.metric)
        raise QueryError(ErrorCode.UNKNOWN_METRIC, "no")

    items = compile_eval.run_part_a([BY_ID["G16"]], compile_fn, FakeRunner(Table((), ())))
    assert [i.id for i in items] == ["G16/npf_ratio", "G16/npf_outstanding_principal"]
    assert seen == ["npf_ratio", "npf_outstanding_principal"]
    assert {i.reason for i in items} == {"compile_error:UNKNOWN_METRIC"}


def test_part_a_reports_a_compile_error_by_code_and_a_service_error_too() -> None:
    def compile_fn(request: QueryRequest) -> CompileResponse:
        raise ServiceError(ServiceErrorCode.CATALOG_NOT_SYNCED, "no catalog")

    (item,) = compile_eval.run_part_a([BY_ID["G01"]], compile_fn, FakeRunner(Table((), ())))
    assert item.reason == "compile_error:CATALOG_NOT_SYNCED"


def test_part_a_reports_a_missing_expected_file(tmp_path: Path) -> None:
    (item,) = compile_eval.run_part_a(
        [BY_ID["G01"]], lambda r: response(), FakeRunner(Table((), ())), expected_dir=tmp_path
    )
    assert item.reason == "no_expected_file"


def test_part_a_only_takes_answer_and_clarify_questions() -> None:
    items = compile_eval.run_part_a(
        [q for q in QUESTIONS if q.outcome in ("no_metric", "refuse")],
        lambda r: response(),
        FakeRunner(Table((), ())),
    )
    assert items == []


# ---- engine errors never reach the report ----


SECRET = "Budi Santoso 3201010101010001"


def test_a_failing_statement_is_reported_by_sqlstate_and_class_not_by_message() -> None:
    failure = SqlFailure("22P02", "InvalidTextRepresentation", f'invalid input syntax: "{SECRET}"')
    logged: list[str] = []
    items = compile_eval.run_part_b(
        [PartBRequest("m", "plain", QueryRequest(metric="m"))],
        lambda r: response(),
        FakeRunner(failure),
        log=logged.append,
    )
    assert [i.reason for i in items] == ["sql_error:22P02:InvalidTextRepresentation"]
    assert SECRET not in str(failure)
    assert SECRET not in repr(part_summary(items))
    # The text goes to the log function only, which the command passes for --verbose.
    assert logged == [f'm: invalid input syntax: "{SECRET}"']


# ---- part B ------


def stored(
    name: str, dimensions: list[dict[str, Any]], grain: str | None, fingerprint: str = "fp"
) -> dict[str, Any]:
    return {
        "name": name,
        "official": {"default_grain": grain} if grain else {},
        "derived": {"dimensions": dimensions, "basis": {"governance_fingerprint": fingerprint}},
    }


def option(ref: str, *, available: bool = True, sensitivity: str = "internal") -> dict[str, Any]:
    return {"ref": ref, "available": available, "sensitivity": sensitivity}


def test_part_b_requests_follow_the_stored_dimensions_and_default_grain() -> None:
    rows = [
        stored(
            "a",
            [
                option("branch.region_name"),
                option("customer.birth_date", sensitivity="personal"),
                option("customer.full_name", available=False, sensitivity="personal"),
                option("product.product_category"),
            ],
            "month",
        ),
        stored("b", [], None),
    ]
    requests = part_b_requests(rows, "fp")
    assert [(r.metric, r.kind, r.dimension) for r in requests] == [
        ("a", "plain", None),
        ("a", "default_grain", None),
        ("a", "dimension", "branch.region_name"),
        ("a", "dimension", "product.product_category"),
        ("b", "plain", None),
    ]
    plain, grain, dimension = requests[0].request, requests[1].request, requests[2].request
    assert (plain.time_start, plain.time_end, plain.dimensions) == ("2026-01-01", "2026-07-01", ())
    assert grain.grain == "month" and grain.time_end == "2026-07-01"
    assert (dimension.time_start, dimension.time_end) == ("2026-06-01", "2026-07-01")
    assert dimension.dimensions == ("branch.region_name",)


def test_part_b_refuses_dimensions_stored_under_another_policy() -> None:
    with pytest.raises(ValueError, match="makna index build"):
        part_b_requests([stored("a", [], None, fingerprint="other")], "fp")
    with pytest.raises(ValueError, match="makna index build"):
        part_b_requests([{"name": "a", "official": {}, "derived": None}], "fp")


def test_part_b_needs_at_least_one_row() -> None:
    entry = PartBRequest("m", "plain", QueryRequest(metric="m"))
    empty = compile_eval.run_part_b([entry], lambda r: response(), FakeRunner(Table(("x",), ())))
    full = compile_eval.run_part_b(
        [entry], lambda r: response(), FakeRunner(Table(("x",), ((1,),)))
    )
    assert [i.reason for i in empty] == ["no_rows"]
    assert [i.reason for i in full] == [None]


def test_part_b_ids_say_what_was_asked() -> None:
    request = QueryRequest(metric="m")
    assert PartBRequest("m", "plain", request).id == "m"
    assert PartBRequest("m", "default_grain", request).id == "m @grain"
    assert (
        PartBRequest("m", "dimension", request, "branch.region_name").id
        == "m by branch.region_name"
    )


# ---- part C ------


def refusing(code: ErrorCode) -> Callable[[QueryRequest], CompileResponse]:
    def compile_fn(request: QueryRequest) -> CompileResponse:
        raise QueryError(code, "refused")

    return compile_fn


def test_part_c_covers_the_requests_of_the_refusal_questions() -> None:
    items = compile_eval.run_part_c(
        QUESTIONS, refusing(ErrorCode.DIMENSION_NOT_AVAILABLE), lambda m: "draft"
    )
    ids = {i.id for i in items}
    wanted = {
        q.id
        for q in QUESTIONS
        if q.outcome in ("no_metric", "refuse") and q.expected.request is not None
    }
    assert ids == wanted and len(wanted) >= 4


def test_part_c_expects_the_code_of_each_reason() -> None:
    by_reason = {
        "dimension_not_available": ErrorCode.DIMENSION_NOT_AVAILABLE,
        "personal_data": ErrorCode.PERSONAL_DATA_REFUSED,
        # An agent token sees the approved snapshot only: a draft is an unknown metric to it.
        "not_approved": ErrorCode.UNKNOWN_METRIC,
    }

    def compile_fn(request: QueryRequest) -> CompileResponse:
        for question in QUESTIONS:
            expected = question.expected
            if expected.request is not None and expected.request.to_query_request() == request:
                raise QueryError(by_reason[expected.reason or ""], "refused")
        raise AssertionError(request)

    items = compile_eval.run_part_c(QUESTIONS, compile_fn, lambda metric: "draft")
    assert [i for i in items if i.reason is not None] == []


def test_part_c_fails_when_no_error_is_raised_or_the_code_is_wrong() -> None:
    questions = [BY_ID["G19"]]
    ok = compile_eval.run_part_c(
        questions, refusing(ErrorCode.DIMENSION_NOT_AVAILABLE), lambda m: None
    )
    wrong = compile_eval.run_part_c(questions, refusing(ErrorCode.INVALID_GRAIN), lambda m: None)
    none = compile_eval.run_part_c(questions, lambda r: response(), lambda m: None)
    assert ok == [Item("G19", None)]
    assert wrong[0].reason == "wrong_error:INVALID_GRAIN:expected:DIMENSION_NOT_AVAILABLE"
    assert none[0].reason == "no_error:expected:DIMENSION_NOT_AVAILABLE"


def test_part_c_checks_that_an_unknown_metric_is_really_a_draft() -> None:
    questions = [BY_ID["G46"]]
    compile_fn = refusing(ErrorCode.UNKNOWN_METRIC)
    assert compile_eval.run_part_c(questions, compile_fn, lambda m: "draft") == [Item("G46", None)]
    (item,) = compile_eval.run_part_c(questions, compile_fn, lambda m: None)
    assert item.reason == "metric_is_not_a_draft"
    (item,) = compile_eval.run_part_c(questions, compile_fn, lambda m: "approved")
    assert item.reason == "metric_is_not_a_draft"


def test_the_summary_counts_and_lists_failures() -> None:
    summary = part_summary([Item("a", None, rows=2), Item("b", "no_rows"), Item("c", None)])
    assert (summary["total"], summary["passed"]) == (3, 2)
    assert summary["pass_rate"] == pytest.approx(2 / 3)
    assert summary["failures"] == [{"id": "b", "reason": "no_rows"}]
    assert part_summary([])["pass_rate"] is None


# ---- running SQL on a database with another name ----


def test_the_catalog_qualifier_is_kept_on_the_database_the_compiler_names() -> None:
    sql = "SELECT 1 FROM lending.core.contract AS c"
    assert runnable_sql(sql, "lending") == sql


def test_the_catalog_qualifier_is_dropped_on_a_copy_under_another_name() -> None:
    sql = "SELECT c.id FROM lending.core.contract AS c JOIN lending.core.branch AS b ON c.b = b.id"
    out = runnable_sql(sql, "wp14_lending")
    assert "lending.core" not in out and "core.contract" in out and "core.branch" in out
