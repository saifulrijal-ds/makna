"""WP-08 criterion 6 (V19) and `makna validate` with the example-request check."""

from __future__ import annotations

import copy
import re
import shutil
from pathlib import Path

import pytest
from sqlglot.errors import ParseError

from makna.cli.main import main
from makna.compiler.compile import compile_plan
from makna.compiler.plan import QueryPlan
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import Dialect
from makna.definitions.loader import load_definitions
from makna.definitions.validate import validate
from makna.model.request import CompileResponse, ErrorCode, QueryError
from makna.services.examples import check_example_requests

from . import definition_fixtures as fx
from .compiler_support import DEFINITIONS, EXAMPLES, metric_doc
from .definition_fixtures import Files


def v19(root: Path) -> list[str]:
    issues = validate(load_definitions(root), extra_checks=[check_example_requests])
    return [i.format() for i in issues if i.code == "V19"]


def all_codes(root: Path) -> list[str]:
    issues = validate(load_definitions(root), extra_checks=[check_example_requests])
    return [i.code for i in issues]


@pytest.mark.parametrize("root", [EXAMPLES, DEFINITIONS], ids=["schema-examples", "definitions"])
def test_every_sample_example_request_passes_v19(root: Path) -> None:
    assert all_codes(root) == []


def test_every_sample_metric_with_an_example_request_is_actually_checked() -> None:
    files = load_definitions(DEFINITIONS).valid_files()
    with_examples = [
        f for f in files if getattr(f.definition, "kind", "") == "metric"
        and getattr(f.definition, "example_requests", None)
    ]  # fmt: skip
    assert len(with_examples) >= 20


def dimension_the_metric_cannot_use(files: Files) -> None:
    metric_doc(files, "revenue")["example_requests"] = [
        {"question": "Revenue by customer email?", "request": {"dimensions": ["order.nope"]}}
    ]


def dimension_not_available(files: Files) -> None:
    """`warehouse` exists, but no relationship leads to it from an order."""
    warehouse = copy.deepcopy(fx._entity(files, "region"))
    warehouse.update(name="warehouse", source={"table": "shop.core.warehouses"})
    warehouse["dimensions"][0]["name"] = "warehouse_name"
    warehouse["dimensions"][0].pop("values")
    files["shop/entities/warehouse.yaml"] = warehouse
    metric_doc(files, "revenue")["example_requests"] = [
        {
            "question": "Revenue by warehouse?",
            "request": {"dimensions": ["warehouse.warehouse_name"]},
        }
    ]


def test_an_example_request_with_a_dimension_the_metric_cannot_use_fails_v19(
    tmp_path: Path,
) -> None:
    root = fx.write(_files(dimension_not_available), tmp_path)
    lines = v19(root)
    assert len(lines) == 1
    assert "revenue.yaml" in lines[0]
    assert "V19" in lines[0] and "DIMENSION_NOT_AVAILABLE" in lines[0], lines


def _files(*mutations: fx.Mutation) -> Files:
    files = copy.deepcopy(fx.base())
    for mutate in mutations:
        mutate(files)
    return files


def test_v19_points_at_the_metric_file_and_the_example_request(tmp_path: Path) -> None:
    root = fx.write(_files(dimension_the_metric_cannot_use), tmp_path)
    (line,) = v19(root)
    match = re.fullmatch(
        r"shop/metrics/revenue\.yaml:(?P<line>\d+): V19 example request 1 \('Revenue by customer "
        r"email\?'\) does not plan: UNKNOWN_DIMENSION: .+",
        line,
    )
    assert match, line
    source = (root / "shop/metrics/revenue.yaml").read_text("utf-8").splitlines()
    assert "request:" in source[int(match["line"]) - 1]


def test_the_second_example_is_reported_by_its_number(tmp_path: Path) -> None:
    def second_is_bad(files: Files) -> None:
        metric_doc(files, "order_count")["time_grains"] = ["month"]
        metric_doc(files, "order_count")["default_grain"] = "month"
        metric_doc(files, "order_count")["example_requests"].append(
            {"question": "Order count per day?", "request": {"grain": "day"}}
        )

    (line,) = v19(fx.write(_files(second_is_bad), tmp_path))
    assert "order_count.yaml" in line and "example request 2" in line and "INVALID_GRAIN" in line


def test_a_proposal_that_is_not_approved_is_checked_without_the_status_gate(
    tmp_path: Path,
) -> None:
    def proposal(files: Files) -> None:
        doc = files.pop("shop/metrics/order_count.yaml")
        doc["status"] = "draft"
        files["shop/metrics/order_count.draft.yaml"] = doc

    assert v19(fx.write(_files(proposal), tmp_path)) == []


def test_a_proposal_is_checked_against_its_own_content(tmp_path: Path) -> None:
    def proposal_with_bad_example(files: Files) -> None:
        official = files["shop/metrics/order_count.yaml"]
        draft = {**official, "status": "draft"}
        draft["example_requests"] = [
            {"question": "Orders by nonsense?", "request": {"dimensions": ["order.nonsense"]}}
        ]
        files["shop/metrics/order_count.draft.yaml"] = draft

    lines = v19(fx.write(_files(proposal_with_bad_example), tmp_path))
    assert len(lines) == 1
    assert "order_count.draft.yaml" in lines[0] and "UNKNOWN_DIMENSION" in lines[0]


def test_a_metric_that_does_not_resolve_gets_no_second_error(tmp_path: Path) -> None:
    def broken(files: Files) -> None:
        metric_doc(files, "revenue")["measure"] = "order.no_such_measure"

    root = fx.write(_files(broken), tmp_path)
    assert v19(root) == []
    assert "V08" in all_codes(root)


# ---- makna validate ---------------------------------------------------------------------


def test_validate_reports_v19_for_a_bad_example_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = fx.write(_files(dimension_the_metric_cannot_use), tmp_path)
    assert main(["validate", str(root)]) == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1 and " V19 " in lines[0]
    assert "UNKNOWN_DIMENSION" in lines[0]
    assert "1 problem" in captured.err


def test_validate_passes_on_the_sample_definitions(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(DEFINITIONS)]) == 0
    assert capsys.readouterr().out == ""


def test_a_sample_example_request_that_asks_for_a_payment_dimension_fails_v19(
    tmp_path: Path,
) -> None:
    root = tmp_path / "definitions"
    shutil.copytree(DEFINITIONS, root)
    path = root / "lending" / "metrics" / "npf_ratio.yaml"
    text = path.read_text(encoding="utf-8")
    assert "dimensions: [branch.region_name]" in text
    path.write_text(
        text.replace("dimensions: [branch.region_name]", "dimensions: [payment.payment_channel]"),
        encoding="utf-8",
    )
    (line,) = v19(root)
    assert line.startswith("lending/metrics/npf_ratio.yaml:") and "DIMENSION_NOT_AVAILABLE" in line


# ---- WP-09: V19 also generates the SQL ---------------------------------------------------


def test_v19_generates_postgres_sql_for_each_example_and_reads_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    def spy(model: Model, plan: QueryPlan, dialect: Dialect = "postgres") -> CompileResponse:
        response = compile_plan(model, plan, dialect)
        printed.append(f"{dialect}:{response.metric.id}")
        return response

    monkeypatch.setattr("makna.services.examples.compile_plan", spy)
    assert all_codes(DEFINITIONS) == []
    files = load_definitions(DEFINITIONS).valid_files()
    expected = sum(
        len(getattr(f.definition, "example_requests", None) or []) for f in files
    )  # fmt: skip
    assert len(printed) == expected >= 20
    assert {entry.split(":")[0] for entry in printed} == {"postgres"}


def test_v19_reports_sql_that_does_not_parse_for_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(sql: str, dialect: Dialect) -> None:
        raise ParseError("Invalid expression / Unexpected token\n second line")

    monkeypatch.setattr("makna.services.examples.reparse", broken)
    lines = v19(fx.write(_files(), tmp_path))
    assert lines and all(
        "does not parse for postgres: Invalid expression" in line for line in lines
    )
    assert all(" V19 " in line for line in lines)


def test_v19_reports_a_failure_while_generating_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(model: Model, plan: QueryPlan, dialect: Dialect = "postgres") -> CompileResponse:
        raise QueryError(ErrorCode.INVALID_FILTER, "cannot be written")

    monkeypatch.setattr("makna.services.examples.compile_plan", fails)
    lines = v19(fx.write(_files(), tmp_path))
    assert lines and all("does not compile to SQL: INVALID_FILTER" in line for line in lines)
