"""WP-15C (C-5, D-32 ruling 2, and N-1, N-2 of the WP-15B reviews): what V05 refuses.

Expressions are written in sqlglot's neutral SQL, not PostgreSQL. V05 refuses what the neutral and
the PostgreSQL readings disagree on, a field selection on a value, and any expression that makes
a printer fail (that last one is an issue, not a crash).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from sqlglot import exp

from makna.compiler import governance as final_check
from makna.compiler.plan import plan_request
from makna.definitions import validate as validation
from makna.definitions.loader import load_definitions
from makna.definitions.validate import validate
from makna.model.expressions import ExpressionError, columns_read
from makna.model.functions import node_problem
from makna.model.request import ErrorCode, QueryError

from . import definition_fixtures as fx
from .compiler_support import request, shop
from .definition_fixtures import Files


def _revenue_issues(expr: str, tmp_path: Path) -> list[tuple[str, str]]:
    files = copy.deepcopy(fx.base())
    measures = files["shop/entities/order.yaml"]["measures"]
    next(m for m in measures if m["name"] == "revenue")["expr"] = expr
    root = fx.write(files, tmp_path)
    return [(i.code, i.message) for i in validate(load_definitions(root))]


# --- what the neutral and the PostgreSQL readings disagree on ----------------------------

DISAGREE = [
    ("total_amount ^ 2", "^"),
    ("CAST(total_amount AS int8)", "int8"),
    ("total_amount::int8", "int8"),
    ("CAST(total_amount AS INT8)", "int8"),
    ("CAST(total_amount AS int4)", "int4"),
    ("total_amount::int4", "int4"),
    ("CAST(total_amount AS int2)", "int2"),
    ("total_amount::int2", "int2"),
    ('CAST(total_amount AS "int8")', "int8"),
    ("CONCAT(status, 'x')", "different"),  # `||` in the neutral reading, CONCAT in PostgreSQL
    ("DATE_PART('year', order_date)", "different"),
    ("BTRIM(status)", "different"),
]


@pytest.mark.parametrize(("expr", "word"), DISAGREE)
def test_v05_refuses_a_spelling_that_means_something_else_in_postgresql(
    expr: str, word: str, tmp_path: Path
) -> None:
    issues = _revenue_issues(expr, tmp_path)
    assert [c for c, _ in issues] == ["V05"], issues
    assert word in issues[0][1], issues


AGREE = [
    "total_amount / item_count",  # division is allowed: the neutral reading adds a cast
    "total_amount / 3",
    "ROUND(total_amount / 3, 2)",
    "CASE WHEN total_amount IS NOT NULL THEN total_amount END",  # IS NOT NULL is the same condition
    "POWER(total_amount, 2)",
    "CAST(total_amount AS BIGINT)",
    "CAST(total_amount AS SMALLINT)",
    "CAST(total_amount AS INTEGER)",
    "total_amount::bigint",
    "total_amount::double precision",
    "COALESCE(total_amount, 0) + 1",
    "EXTRACT(YEAR FROM order_date)",
    "total_amount * 2 - 1",
]


@pytest.mark.parametrize("expr", AGREE)
def test_v05_accepts_what_both_readings_agree_on(expr: str, tmp_path: Path) -> None:
    assert _revenue_issues(expr, tmp_path) == []


def test_a_quoted_column_called_int8_is_a_string_not_a_type(tmp_path: Path) -> None:
    # a string constant that spells a type name is data, not a type
    assert _revenue_issues("CASE WHEN status = 'int8' THEN 1 ELSE 0 END", tmp_path) == []


def test_the_sample_definitions_still_validate() -> None:
    from makna.services.examples import check_example_requests

    from .compiler_support import DEFINITIONS

    issues = validate(load_definitions(DEFINITIONS), extra_checks=[check_example_requests])
    assert [i.format() for i in issues] == []


# --- N-2: a printer that fails is an issue, not a crash -----------------------------------


def test_datetime_function_is_a_v05_issue_not_a_crash(tmp_path: Path) -> None:
    """sqlglot's DuckDB printer raises AttributeError on `DATETIME(x)`; any exception of the print
    loop must come out as a V05 issue."""
    issues = _revenue_issues("DATETIME(order_date)", tmp_path)
    assert [c for c, _ in issues] == ["V05"], issues


# --- the raw-text check of `_parse` ----------------------------------------------------------


def test_parse_itself_refuses_raw_text() -> None:
    """The allow-list pass (`node_problem`) refuses the same text with another message, so the
    check in `_parse` can only be seen by calling it: without it, `_parse` returns the tree."""
    with pytest.raises(validation._ExprError, match="kept as raw text"):
        validation._parse("IF total_amount > 1 THEN 1")


def test_v05_names_raw_text_as_such(tmp_path: Path) -> None:
    issues = _revenue_issues("IF total_amount > 1 THEN 1", tmp_path)
    assert [c for c, _ in issues] == ["V05"], issues
    assert "kept as raw text" in issues[0][1]


# --- N-1: field selection on a value ---------------------------------------------------------

FIELD_SELECTION = ["(segment).email", "(any_value).email", "(email).domain", "(x).y.z", "(a).*"]


@pytest.mark.parametrize("sql", FIELD_SELECTION)
def test_columns_read_refuses_field_selection(sql: str) -> None:
    with pytest.raises(ExpressionError, match="field"):
        columns_read(sql)


@pytest.mark.parametrize("sql", FIELD_SELECTION)
def test_the_allow_list_refuses_field_selection(sql: str) -> None:
    import sqlglot

    tree = sqlglot.parse_one(sql, into=exp.Condition)
    dot = next(tree.find_all(exp.Dot))
    assert node_problem(dot) == "field selection on a value"


@pytest.mark.parametrize("sql", FIELD_SELECTION)
def test_v05_refuses_field_selection(sql: str, tmp_path: Path) -> None:
    issues = _revenue_issues(sql, tmp_path)
    assert "V05" in {c for c, _ in issues}, issues


def test_field_selection_on_a_personal_column_is_not_a_way_around_v20(tmp_path: Path) -> None:
    def mutate(files: Files) -> None:
        files["shop/entities/customer.yaml"]["dimensions"].append(
            fx._dim("mail_field", "categorical", expr="(email).domain", sensitivity="internal")
        )

    files = copy.deepcopy(fx.base())
    mutate(files)
    issues = validate(load_definitions(fx.write(files, tmp_path)))
    assert {"V05", "V20"} <= {i.code for i in issues}


def test_the_planner_refuses_a_dimension_that_selects_a_field() -> None:
    def mutate(files: Files) -> None:
        files["shop/entities/customer.yaml"]["dimensions"].append(
            fx._dim("mail_field", "categorical", expr="(email).domain", sensitivity="internal")
        )

    with pytest.raises(QueryError) as caught:
        plan_request(shop(mutate), request("revenue"))
    assert caught.value.code is ErrorCode.BROKEN_REFERENCE


def test_the_final_check_refuses_field_selection_in_the_statement() -> None:
    import sqlglot

    plan = plan_request(shop(), request("order_count"))
    selected = sqlglot.parse_one("(email).domain", into=exp.Condition)
    tree = exp.select(exp.alias_(selected, "leak", quoted=True)).from_("t")
    with pytest.raises(final_check.GovernanceError, match="field selection"):
        final_check.verify(tree, plan)


# --- I-1: date functions the engines read differently ----------------------------------------

DATE_REFUSED = [
    "DATE_ADD(order_date, 1)",
    "DATE_SUB(order_date, 1)",
    "TS_OR_DS_ADD(order_date, 1)",
    "DATEDIFF(order_date, order_date)",
    "DATE_DIFF(order_date, order_date)",
    "DAYOFWEEK(order_date)",
    "DAYOFMONTH(order_date)",
    "DAYOFYEAR(order_date)",
    "QUARTER(order_date)",
    "WEEK(order_date)",
]
DATE_ACCEPTED = [
    "DATE_ADD(order_date, 1, 'day')",
    "DATE_ADD(order_date, INTERVAL 1 DAY)",
    "order_date + INTERVAL '1 day'",
    "TS_OR_DS_ADD(order_date, 2, 'day')",
    "EXTRACT(DOW FROM order_date)",
]


@pytest.mark.parametrize("expr", DATE_REFUSED)
def test_v05_refuses_date_functions_that_postgresql_and_duckdb_read_differently(
    expr: str, tmp_path: Path
) -> None:
    issues = _revenue_issues(expr, tmp_path)
    assert [c for c, _ in issues] == ["V05"], issues


@pytest.mark.parametrize("expr", DATE_ACCEPTED)
def test_v05_accepts_date_arithmetic_with_an_explicit_unit(expr: str, tmp_path: Path) -> None:
    assert _revenue_issues(expr, tmp_path) == []


@pytest.mark.parametrize("expr", DATE_REFUSED)
def test_the_final_check_refuses_them_too(expr: str) -> None:
    import sqlglot

    tree = sqlglot.parse_one(expr, into=exp.Condition)
    assert any(node_problem(node) for node in tree.walk()), expr


# --- M-1: subscripting a value ------------------------------------------------------------------

SUBSCRIPTS = ["segment['email']", "x[1]", "(ARRAY[1, 2])[1]"]


@pytest.mark.parametrize("sql", SUBSCRIPTS)
def test_subscripting_is_refused_everywhere(sql: str, tmp_path: Path) -> None:
    import sqlglot

    with pytest.raises(ExpressionError, match="subscripts"):
        columns_read(sql)
    tree = sqlglot.parse_one(sql, into=exp.Condition)
    bracket = next(tree.find_all(exp.Bracket))
    assert node_problem(bracket) == "subscripting a value"
    assert "V05" in {c for c, _ in _revenue_issues(sql, tmp_path)}


def test_the_final_check_refuses_a_subscript_in_the_statement() -> None:
    import sqlglot

    plan = plan_request(shop(), request("order_count"))
    tree = exp.select(
        exp.alias_(sqlglot.parse_one("segment['email']", into=exp.Condition), "leak", quoted=True)
    ).from_("t")
    with pytest.raises(final_check.GovernanceError, match="subscripting"):
        final_check.verify(tree, plan)
