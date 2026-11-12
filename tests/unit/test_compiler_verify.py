"""WP-10 criterion 5: `governance.verify` on hand-built trees.

A column read by any personal dimension of the model, selected or not, may appear only inside a
mask, `COUNT(...)`, or `COUNT(DISTINCT ...)`, and only in the projection or the GROUP BY. The
trees are built here with sqlglot; the tables are the sample `customers` and `contracts`.
"""

from __future__ import annotations

import pytest
from sqlglot import exp

from makna.compiler import compile as compile_module
from makna.compiler import governance
from makna.compiler.compile import compile_request
from makna.compiler.governance import GovernanceError, mask_expression, verify
from makna.compiler.plan import QueryPlan, plan_request
from makna.compiler.sqlgen import build_tree
from makna.model.definitions import MaskBucket, MaskRedact, MaskTruncateDate

from .compiler_support import lending, request
from .test_compiler_refusals import with_derived

CUSTOMERS = ("lending", "core", "customers")


def customers(alias: str | None = "c") -> exp.Table:
    return exp.table_(
        CUSTOMERS[2],
        db=CUSTOMERS[1],
        catalog=CUSTOMERS[0],
        alias=exp.to_identifier(alias) if alias else None,
    )


def col(name: str, table: str | None = "c") -> exp.Column:
    return exp.column(name, table=table)


def select(*items: exp.Expr, table: exp.Table | None = None) -> exp.Select:
    return exp.select(*items).from_(table if table is not None else customers())


def plan(model=None) -> QueryPlan:  # type: ignore[no-untyped-def]
    return plan_request(model or lending(), request("outstanding_principal"))


YEAR = MaskTruncateDate(method="truncate_date", grain="year")


# ---- the cases of the acceptance criterion ---------------------------------------------


def test_a_bare_personal_column_fails() -> None:
    with pytest.raises(GovernanceError, match="birth_date"):
        verify(select(col("birth_date")), plan())


@pytest.mark.parametrize("aggregate", [exp.Min, exp.Max, exp.Sum, exp.Avg])
def test_a_personal_column_inside_min_max_or_another_aggregate_fails(
    aggregate: type[exp.AggFunc],
) -> None:
    with pytest.raises(GovernanceError):
        verify(select(aggregate(this=col("birth_date"))), plan())


def masked_plan() -> QueryPlan:
    """A plan that selects the masked dimension `customer.birth_year`."""
    return plan_request(
        lending(), request("outstanding_principal", dimensions=["customer.birth_year"])
    )


def test_a_personal_column_inside_the_mask_the_plan_built_passes() -> None:
    mask = mask_expression(YEAR, col("birth_date"))
    verify(select(mask.as_("customer__birth_year")), masked_plan())


def test_a_bucket_mask_the_plan_built_passes() -> None:
    from .test_compiler_masking import with_numeric_personal

    model = with_numeric_personal(lending())
    bucketed = plan_request(model, request("new_customer_count", dimensions=["customer.score"]))
    mask = mask_expression(MaskBucket(method="bucket", width=10), col("birth_date"))
    verify(select(mask.as_("customer__score")), bucketed)


def test_a_mask_shaped_expression_that_the_plan_did_not_build_gets_no_exemption() -> None:
    mask = mask_expression(YEAR, col("birth_date"))
    with pytest.raises(GovernanceError):  # no masked dimension in this plan
        verify(select(mask.as_("customer__birth_year")), plan())
    with pytest.raises(GovernanceError):  # a select item that is not a planned masked column
        verify(select(mask.as_("something_else")), masked_plan())


def test_the_mask_under_an_alias_must_be_the_masking_rule_of_that_dimension() -> None:
    month = MaskTruncateDate(method="truncate_date", grain="month")
    with pytest.raises(GovernanceError, match="masking rule"):
        verify(
            select(mask_expression(month, col("birth_date")).as_("customer__birth_year")),
            masked_plan(),
        )
    bucket = mask_expression(MaskBucket(method="bucket", width=10), col("birth_date"))
    with pytest.raises(GovernanceError):
        verify(select(bucket.as_("customer__birth_year")), masked_plan())


def test_a_bucket_of_another_width_under_a_bucket_alias_fails() -> None:
    from .test_compiler_masking import with_numeric_personal

    model = with_numeric_personal(lending())
    bucketed = plan_request(model, request("new_customer_count", dimensions=["customer.score"]))
    five = mask_expression(MaskBucket(method="bucket", width=5), col("birth_date"))
    with pytest.raises(GovernanceError):
        verify(select(five.as_("customer__score")), bucketed)


@pytest.mark.parametrize("wrapper", [exp.Max, exp.Min, exp.Sum, exp.Count])
def test_a_mask_shaped_expression_inside_an_aggregate_fails(wrapper: type[exp.AggFunc]) -> None:
    bucket = mask_expression(MaskBucket(method="bucket", width=1), col("birth_date"))
    with pytest.raises(GovernanceError):
        verify(select(wrapper(this=bucket).as_("m")), masked_plan())


def test_a_bucket_wrapped_case_inside_count_fails() -> None:
    case = exp.Case(
        ifs=[
            exp.If(
                this=exp.LT(this=col("birth_date"), expression=exp.Null()),
                true=exp.Literal.number(1),
            )
        ]
    )
    bucket = mask_expression(MaskBucket(method="bucket", width=1), case)
    with pytest.raises(GovernanceError):
        verify(select(exp.Count(this=bucket).as_("m")), masked_plan())


def test_a_personal_column_inside_count_passes() -> None:
    verify(select(exp.Count(this=col("email"))), plan())


def test_a_personal_column_inside_count_distinct_passes() -> None:
    verify(select(exp.Count(this=exp.Distinct(expressions=[col("national_id")]))), plan())


def test_a_mask_in_the_group_by_passes_when_a_planned_select_item_has_the_same_mask() -> None:
    mask = mask_expression(YEAR, col("birth_date"))
    tree = select(mask.as_("customer__birth_year"), exp.Count(this=exp.Star())).group_by(
        mask.copy()
    )
    verify(tree, masked_plan())
    other = mask_expression(
        MaskTruncateDate(method="truncate_date", grain="month"), col("birth_date")
    )
    with pytest.raises(GovernanceError):  # a group-by item that no planned select item shows
        verify(select(exp.Count(this=exp.Star())).group_by(other), masked_plan())


def test_the_error_is_internal_not_a_caller_error() -> None:
    from makna.model.request import QueryError

    with pytest.raises(GovernanceError) as caught:
        verify(select(col("email")), plan())
    assert not isinstance(caught.value, QueryError)


# ---- only the bare column may be counted ------------------------------------------------


def test_a_count_of_an_expression_over_a_personal_column_fails() -> None:
    predicate = exp.Case(
        ifs=[
            exp.If(
                this=exp.LT(this=col("birth_date"), expression=exp.Null()),
                true=exp.Literal.number(1),
            )
        ]
    )
    with pytest.raises(GovernanceError):
        verify(select(exp.Count(this=predicate)), plan())


def test_a_distinct_count_of_a_comparison_fails() -> None:
    comparison = exp.GT(this=col("birth_date"), expression=exp.Literal.number(1))
    with pytest.raises(GovernanceError):
        verify(select(exp.Count(this=exp.Distinct(expressions=[comparison]))), plan())


def test_a_count_of_a_function_of_a_personal_column_fails() -> None:
    with pytest.raises(GovernanceError):
        verify(select(exp.Count(this=exp.Lower(this=col("email")))), plan())


def test_a_distinct_count_of_two_expressions_fails() -> None:
    both = exp.Distinct(expressions=[col("email"), col("province")])
    with pytest.raises(GovernanceError):
        verify(select(exp.Count(this=both)), plan())


# ---- every personal column of the model, not only the selected ones ---------------------


@pytest.mark.parametrize(
    "name", ["full_name", "national_id", "email", "phone_number", "birth_date"]
)
def test_every_personal_column_of_the_model_is_checked(name: str) -> None:
    with pytest.raises(GovernanceError, match=name):
        verify(select(col(name)), plan())  # the plan selects no customer dimension at all


def test_ordinary_columns_of_the_same_table_pass() -> None:
    verify(select(col("province"), col("city"), col("customer_id")), plan())


def test_a_column_made_personal_by_the_catalog_is_checked() -> None:
    ordinary = plan()
    verify(select(col("province")), ordinary)
    derived = plan(with_derived(lending(), {"lending.core.customers.province": "personal"}))
    with pytest.raises(GovernanceError, match="province"):
        verify(select(col("province")), derived)


def test_a_column_read_through_an_expression_is_checked() -> None:
    # birth_year reads `birth_date`; the dimension's own column is what counts.
    tree = select(exp.Cast(this=col("birth_date"), to=exp.DataType.build("date")))
    with pytest.raises(GovernanceError):
        verify(tree, plan())


# ---- where a column may sit --------------------------------------------------------------


def test_a_filter_on_a_personal_column_fails() -> None:
    with pytest.raises(GovernanceError):
        verify(
            select(exp.Literal.number(1)).where(
                exp.EQ(this=col("email"), expression=exp.Literal.string("x"))
            ),
            plan(),
        )


def test_a_masked_or_counted_personal_column_in_a_filter_still_fails() -> None:
    masked = exp.EQ(this=mask_expression(YEAR, col("birth_date")), expression=exp.Null())
    with pytest.raises(GovernanceError):
        verify(select(exp.Literal.number(1)).where(masked), masked_plan())
    counted = exp.GT(this=exp.Count(this=col("email")), expression=exp.Literal.number(1))
    with pytest.raises(GovernanceError):
        verify(select(exp.Literal.number(1)).having(counted), plan())


def test_a_personal_column_in_a_join_condition_fails() -> None:
    other = exp.table_("contracts", db="core", catalog="lending", alias=exp.to_identifier("k"))
    on = exp.EQ(this=col("national_id"), expression=col("customer_id", "k"))
    tree = select(exp.Literal.number(1)).join(exp.Join(this=other, on=on, side="LEFT"))
    with pytest.raises(GovernanceError):
        verify(tree, plan())


def test_a_personal_column_in_order_by_fails() -> None:
    tree = select(exp.Literal.number(1)).order_by(exp.Ordered(this=col("full_name")))
    with pytest.raises(GovernanceError):
        verify(tree, plan())


def test_a_personal_column_inside_a_subquery_fails() -> None:
    inner = select(col("email"))
    outer = exp.select(exp.Star()).from_(inner.subquery("x"))
    with pytest.raises(GovernanceError):
        verify(outer, plan())


def test_a_count_in_an_outer_query_does_not_excuse_a_bare_column_in_a_subquery() -> None:
    inner = select(col("email"))
    outer = exp.select(exp.Count(this=exp.Star())).from_(inner.subquery("x"))
    with pytest.raises(GovernanceError):
        verify(outer, plan())


def test_an_unqualified_personal_column_is_found() -> None:
    with pytest.raises(GovernanceError):
        verify(select(exp.column("email")), plan())


def test_a_table_name_without_an_alias_is_understood() -> None:
    tree = exp.select(col("email", "customers")).from_(customers(None))
    with pytest.raises(GovernanceError):
        verify(tree, plan())


def test_select_star_from_a_personal_table_fails_but_count_star_passes() -> None:
    with pytest.raises(GovernanceError):
        verify(select(exp.Star()), plan())
    with pytest.raises(GovernanceError):
        verify(select(exp.Column(this=exp.Star(), table=exp.to_identifier("c"))), plan())
    verify(select(exp.Count(this=exp.Star())), plan())


def test_a_column_with_the_same_name_on_another_table_passes() -> None:
    other = exp.table_("contracts", db="core", catalog="lending", alias=exp.to_identifier("k"))
    verify(exp.select(col("email", "k")).from_(other), plan())


def test_a_name_in_an_outer_query_over_a_cte_passes() -> None:
    inner = select(mask_expression(MaskRedact(method="redact"), col("email")).as_("email"))
    outer = exp.select(exp.column("email")).from_("m").with_("m", as_=inner)
    verify(outer, plan())


# ---- masks must be real masks ------------------------------------------------------------


def test_a_truncation_to_the_day_is_not_a_mask() -> None:
    day = exp.Cast(
        this=exp.TimestampTrunc(this=col("birth_date"), unit=exp.Literal.string("day")),
        to=exp.DataType.build("date"),
    )
    with pytest.raises(GovernanceError):
        verify(select(day), plan())


def test_a_cast_alone_is_not_a_mask() -> None:
    with pytest.raises(GovernanceError):
        verify(select(exp.Cast(this=col("birth_date"), to=exp.DataType.build("date"))), plan())


def test_a_bucket_whose_two_widths_differ_is_not_a_mask() -> None:
    broken = exp.Mul(
        this=exp.Floor(
            this=exp.Div(
                this=exp.Cast(this=col("birth_date"), to=exp.DataType.build("double")),
                expression=exp.Literal.number(10),
            )
        ),
        expression=exp.Literal.number(1),
    )
    with pytest.raises(GovernanceError):
        verify(select(broken), plan())


# ---- compile_plan runs it ----------------------------------------------------------------


def test_every_compile_runs_the_final_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def leaky(model, the_plan):  # type: ignore[no-untyped-def]
        return select(col("email"))

    monkeypatch.setattr(compile_module, "build_tree", leaky)
    with pytest.raises(GovernanceError):
        compile_request(lending(), request("outstanding_principal"))


def test_a_masked_request_passes_the_final_check() -> None:
    the_plan = plan_request(
        lending(), request("outstanding_principal", dimensions=["customer.birth_year"])
    )
    verify(build_tree(lending(), the_plan), the_plan)
    assert governance.GROUPS_LEFT_OUT.startswith("__")
