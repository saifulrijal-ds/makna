"""WP-15B criterion 3 (C-7): an expression that does not parse fails closed in every caller.

`makna.model.expressions.columns_read` raises `ExpressionError` when the text does not parse.
Each caller below turns that into a refusal or a problem. The bad expression is built in each
test (a sample file must stay valid). Before WP-15B the callers disagreed: the sensitivity copy
answered "the dimension's own name" and the others "no columns", so a check passed because the
parse failed.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlglot import exp

from makna.catalog.references import CatalogView, find_issues
from makna.compiler import governance as final_check
from makna.compiler.compile import compile_request
from makna.compiler.plan import plan_request
from makna.definitions.loader import load_definitions
from makna.definitions.validate import validate
from makna.model.definitions import Dimension, Entity
from makna.model.expressions import ExpressionError, columns_read, dimension_columns
from makna.model.functions import node_problem
from makna.model.governance import STRICT
from makna.model.request import ErrorCode, QueryError
from makna.model.sensitivity import effective_level

from . import definition_fixtures as fx
from .compiler_support import request, shop
from .definition_fixtures import Files

BAD = "total_amount +"  # does not parse as one SQL expression


# --- the shared function -----------------------------------------------------------------


def test_columns_read_lists_the_columns_once_in_lower_case() -> None:
    assert columns_read("CAST(Registered_At AS DATE)") == ("registered_at",)
    assert columns_read("sts_putusan IN ('a', 'b')") == ("sts_putusan",)
    assert columns_read("NOT is_reversed") == ("is_reversed",)
    assert columns_read("a + A * b") == ("a", "b")
    assert columns_read(None) == ()
    assert columns_read("1") == ()


@pytest.mark.parametrize("sql", ["a b c (", BAD, "", "CASE WHEN"])
def test_columns_read_raises_for_text_that_does_not_parse(sql: str) -> None:
    with pytest.raises(ExpressionError):
        columns_read(sql)


def _dimension(name: str, expr: str | None) -> Dimension:
    return Dimension.model_validate(
        {"name": name, "type": "categorical", "description": "A test dimension.", "expr": expr}
        if expr is not None
        else {"name": name, "type": "categorical", "description": "A test dimension."}
    )


def test_dimension_columns_reads_the_own_name_without_an_expression() -> None:
    assert dimension_columns(_dimension("city", None)) == ("city",)
    assert dimension_columns(_dimension("band", "lower(Amount)")) == ("amount",)
    with pytest.raises(ExpressionError):
        dimension_columns(_dimension("band", BAD))


# --- validation: V20, V18, V12 -----------------------------------------------------------


def _issues(mutation: Callable[[Files], None], tmp_path: Path) -> list[tuple[str, str]]:
    files = copy.deepcopy(fx.base())
    mutation(files)
    root = fx.write(files, tmp_path)
    return [(i.code, i.message) for i in validate(load_definitions(root))]


def _customer(files: Files) -> dict[str, Any]:
    return files["shop/entities/customer.yaml"]


def _codes(issues: list[tuple[str, str]]) -> set[str]:
    return {code for code, _ in issues}


def test_v20_a_non_personal_dimension_that_does_not_parse_is_reported(tmp_path: Path) -> None:
    def mutate(files: Files) -> None:
        _customer(files)["dimensions"].append(
            fx._dim("birth_copy", "time", expr="birth_date +", sensitivity="internal")
        )

    issues = _issues(mutate, tmp_path)
    assert {"V05", "V20"} <= _codes(issues), issues
    assert any(c == "V20" and "birth_copy" in m for c, m in issues)


def test_v18_a_rule_that_does_not_parse_is_reported(tmp_path: Path) -> None:
    def mutate(files: Files) -> None:
        _customer(files)["rules"] = [
            {"name": "mailed", "description": "A customer with an address.", "expr": "email <>"}
        ]

    issues = _issues(mutate, tmp_path)
    assert {"V05", "V18"} <= _codes(issues), issues
    assert any(c == "V18" and "mailed" in m for c, m in issues)


def test_v12_a_measure_filter_or_expression_that_does_not_parse_is_reported(
    tmp_path: Path,
) -> None:
    def in_filter(files: Files) -> None:
        _customer(files)["measures"].append(
            fx._measure("mailed", "count", "registered_date", filter="email <>")
        )

    def in_expr(files: Files) -> None:
        _customer(files)["measures"].append(
            fx._measure("mails", "count", "registered_date", expr="CASE WHEN email")
        )

    for mutation, part in ((in_filter, "filter"), (in_expr, "expression")):
        issues = _issues(mutation, tmp_path / part)
        assert {"V05", "V12"} <= _codes(issues), (part, issues)
        assert any(c == "V12" and part in m for c, m in issues), (part, issues)


def test_the_checks_fail_closed_when_a_personal_dimension_does_not_parse(tmp_path: Path) -> None:
    """The personal columns of the entity cannot be listed, so a rule that parses cannot be
    shown not to read one of them."""

    def mutate(files: Files) -> None:
        _customer(files)["dimensions"][2]["expr"] = "lower(email"  # the personal one
        _customer(files)["rules"] = [
            {"name": "mailed", "description": "A customer with an address.", "expr": "city <> ''"}
        ]

    issues = _issues(mutate, tmp_path)
    assert any(c == "V18" and "mailed" in m for c, m in issues), issues


def test_nothing_is_reported_beyond_v05_when_no_personal_data_is_at_stake(
    tmp_path: Path,
) -> None:
    def mutate(files: Files) -> None:
        files["shop/entities/order.yaml"]["rules"][0]["expr"] = "total_amount >"

    issues = _issues(mutate, tmp_path)
    assert _codes(issues) == {"V05"}, issues


# --- reference check: R02, R03 -----------------------------------------------------------

TABLE = "lending.core.things"


def _entity_with(dimension: dict[str, Any]) -> Entity:
    from ..catalog_support import make_entity

    return make_entity(dimensions=[dimension])


def _view() -> CatalogView:
    return CatalogView({TABLE: {"thing_id": None, "city": None, "amount": None}}, frozenset())


def _codes_of_findings(entity: Entity) -> list[tuple[str, str]]:
    return [(f.code, f.part) for f in find_issues([entity], [], _view())]


def test_r02_and_r03_report_a_dimension_that_does_not_parse() -> None:
    from ..catalog_support import dimension

    found = _codes_of_findings(_entity_with(dimension("band", BAD)))
    assert ("R02", "dimension:band") in found
    assert ("R03", "dimension:band") in found


def test_r02_reports_a_measure_and_a_rule_that_do_not_parse() -> None:
    from ..catalog_support import dimension, make_entity

    entity = make_entity(
        dimensions=[dimension("city")],
        measures=[
            {
                "name": "total",
                "agg": "sum",
                "expr": "amount",
                "filter": "city IS NOT",
                "time_dimension": "city",
                "description": "Sum of the amount of every thing.",
            }
        ],
        rules=[{"name": "big", "description": "A thing with a large amount.", "expr": "amount >"}],
    )
    assert _codes_of_findings(entity) == [("R02", "measure:total"), ("R02", "rule:big")]


# --- the planner and the final check -----------------------------------------------------


def _broken_shop() -> Any:
    def mutate(files: Files) -> None:
        files["shop/entities/order.yaml"]["dimensions"].append(
            fx._dim("band", "categorical", expr=BAD)
        )

    return shop(mutate)


def test_the_planner_refuses_when_a_dimension_expression_does_not_parse() -> None:
    model = _broken_shop()
    with pytest.raises(QueryError) as caught:
        plan_request(model, request("order_count"))
    assert caught.value.code is ErrorCode.BROKEN_REFERENCE
    assert "order.band" in caught.value.message


def test_the_sensitivity_of_a_dimension_that_does_not_parse_is_the_strictest() -> None:
    model = _broken_shop()
    order = model.entities["order"]
    band = next(d for d in order.dimensions if d.name == "band")
    assert effective_level(order, band, {}) == "personal"


def test_the_final_check_has_no_personal_column_list_for_such_a_model() -> None:
    """`verify` reads `plan.personal_columns`; the list it is given is never one with a gap."""
    model = _broken_shop()
    with pytest.raises(ExpressionError):
        final_check.personal_columns(model)
    with pytest.raises(ExpressionError):
        final_check.raw_returnable_columns(model, STRICT)
    with pytest.raises(QueryError) as caught:
        compile_request(model, request("order_count"))
    assert caught.value.code is ErrorCode.BROKEN_REFERENCE


def test_a_model_whose_expressions_parse_is_planned_as_before() -> None:
    model = shop()
    plan = plan_request(model, request("order_count"))
    assert ("shop.core.customers", "email") in plan.personal_columns


# --- text that sqlglot keeps as raw text (exp.Command) -----------------------------------
# sqlglot gives up on some texts, for example one that starts with `IF`, and keeps them as raw
# text that the printer writes out unchanged. Such a node reads no columns, and PostgreSQL may
# read some (a table with a column called `if`): it must be refused everywhere.

RAW = ["IF || email", "IF email", "IF.email", "if email = 'a' then 1 end", "IF 1, email"]


@pytest.mark.parametrize("sql", RAW)
def test_text_kept_as_raw_text_does_not_read_columns_it_raises(sql: str) -> None:
    with pytest.raises(ExpressionError):
        columns_read(sql)


@pytest.mark.parametrize("sql", RAW)
def test_v05_and_v20_refuse_raw_text_in_a_dimension(sql: str, tmp_path: Path) -> None:
    def mutate(files: Files) -> None:
        _customer(files)["dimensions"].append(
            fx._dim("leak", "categorical", expr=sql, sensitivity="internal")
        )

    issues = _issues(mutate, tmp_path)
    assert {"V05", "V20"} <= _codes(issues), issues


@pytest.mark.parametrize("sql", RAW)
def test_r02_refuses_raw_text_in_a_dimension(sql: str) -> None:
    from ..catalog_support import dimension

    assert ("R02", "dimension:leak") in _codes_of_findings(_entity_with(dimension("leak", sql)))


def _model_with_leak(sql: str) -> Any:
    def mutate(files: Files) -> None:
        files["shop/entities/customer.yaml"]["dimensions"].append(
            fx._dim("leak", "categorical", expr=sql, sensitivity="internal")
        )

    return shop(mutate)


@pytest.mark.parametrize("sql", RAW)
def test_the_planner_refuses_raw_text_in_a_dimension(sql: str) -> None:
    with pytest.raises(QueryError) as caught:
        plan_request(_model_with_leak(sql), request("revenue", dimensions=["customer.leak"]))
    assert caught.value.code is ErrorCode.BROKEN_REFERENCE


def test_the_final_check_refuses_raw_text_in_the_statement() -> None:
    plan = plan_request(shop(), request("order_count"))
    raw = exp.Command(this="IF", expression=" || email")
    tree = exp.select(exp.alias_(raw, "leak", quoted=True)).from_("t")
    with pytest.raises(final_check.GovernanceError, match="raw text"):
        final_check.verify(tree, plan)
    assert node_problem(raw) is not None


# --- the refusal for a dimension that does not parse -------------------------------------


def test_the_refusal_names_a_problem_in_the_definitions_and_never_quotes_the_expression() -> None:
    with pytest.raises(QueryError) as caught:
        plan_request(_broken_shop(), request("order_count"))
    error = caught.value
    assert error.message.startswith("A definition in the model cannot be read")
    assert "dimension order.band" in error.message
    assert "this metric depends on" not in error.message
    assert BAD not in error.message and BAD not in str(error.details)
    assert error.details["issues"][0]["object"] == "dimension:order.band"


# --- V12 when a personal dimension does not parse ----------------------------------------


def test_v12_says_expression_not_filter_and_allows_counting_a_bare_column(tmp_path: Path) -> None:
    def mutate(files: Files) -> None:
        _customer(files)["dimensions"][2]["expr"] = "lower(email"  # the personal one
        _customer(files)["measures"].append(
            fx._measure("mixed", "count", "registered_date", expr="CASE WHEN city > 'm' THEN 1 END")
        )

    issues = _issues(mutate, tmp_path)
    v12 = [m for c, m in issues if c == "V12"]
    assert len(v12) == 1 and "measure mixed" in v12[0], v12  # not signups, not distinct_emails
    assert "its expression cannot be shown" in v12[0] and "filter" not in v12[0]


def test_v12_reports_the_gap_when_the_expression_also_reads_a_known_personal_column(
    tmp_path: Path,
) -> None:
    def mutate(files: Files) -> None:
        _customer(files)["dimensions"][2]["expr"] = "lower(email"
        _customer(files)["measures"].append(
            fx._measure(
                "both", "count", "registered_date", expr="CASE WHEN birth_date > city THEN 1 END"
            )
        )

    issues = _issues(mutate, tmp_path)
    assert any(c == "V12" and "both" in m and "cannot be shown" in m for c, m in issues), issues
