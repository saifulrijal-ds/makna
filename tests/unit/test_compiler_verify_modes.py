"""WP-10B criterion 4 (D-29): `governance.verify` follows the policy of the plan.

Each case is a hand-built tree over the sample `customers` table. For each policy the table says
whether `verify` passes it. `strict` must reject exactly what it rejected before the modes
(`test_compiler_verify.py` has the full WP-10 set). `custom` accepts a raw personal column only
where an enabled rule allows it and the plan put it there: as a whole select item or GROUP BY item
under the alias of a dimension the plan selected raw, and inside the WHERE of a query of its own.
`off` is `custom` with every rule relaxed and is checked the same way: it skips nothing. The plan
of each case selects `customer.birth_year` and, where the policy allows unmasked personal data,
the four unmasked personal dimensions.

Policies: `strict`; `filters` (only `allow_personal_filters`); `unmasked` (only
`allow_unmasked_personal`); `raw_masks` (only `apply_masks = false`); `no_minimum` (only
`min_group_size = 0`); `all_custom` (the three booleans relaxed in `custom`); `off`.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlglot import exp

from makna.compiler.governance import GovernanceError, mask_expression, verify
from makna.compiler.plan import QueryPlan, plan_request
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import build_tree
from makna.model.definitions import Dimension, MaskTruncateDate
from makna.model.governance import STRICT, GovernancePolicy
from makna.model.request import QueryError

from .compiler_support import lending, request
from .test_compiler_refusals import with_derived
from .test_compiler_verify import col, customers, select

POLICIES: dict[str, GovernancePolicy] = {
    "strict": STRICT,
    "filters": GovernancePolicy.custom(allow_personal_filters=True),
    "unmasked": GovernancePolicy.custom(allow_unmasked_personal=True),
    "raw_masks": GovernancePolicy.custom(apply_masks=False),
    "no_minimum": GovernancePolicy.custom(min_group_size=0),
    "all_custom": GovernancePolicy.custom(
        allow_personal_filters=True, apply_masks=False, allow_unmasked_personal=True
    ),
    "off": GovernancePolicy.off(),
}
ALL = frozenset(POLICIES)
NONE: frozenset[str] = frozenset()
# Where a raw column of a dimension with no masking rule (full_name) may be returned.
NO_MASK_SELECT = frozenset({"unmasked", "all_custom", "off"})
# Where a raw column of a dimension with a masking rule (birth_date) may be returned.
MASKED_SELECT = frozenset({"raw_masks", "all_custom", "off"})
WHERE = frozenset({"filters", "all_custom", "off"})
YEAR = MaskTruncateDate(method="truncate_date", grain="year")
UNMASKED_DIMENSIONS = [
    "customer.full_name",
    "customer.national_id",
    "customer.email",
    "customer.phone_number",
]


def plan_for(policy: GovernancePolicy, model: Model | None = None) -> QueryPlan:
    """A plan that selects what the policy lets it select: the birth year, and the unmasked
    personal dimensions when the policy allows them."""
    dimensions = ["customer.birth_year"]
    if policy.allow_unmasked_personal:
        dimensions += UNMASKED_DIMENSIONS
    return plan_request(
        model or lending(),
        request("new_customer_count", dimensions=dimensions),
        governance=policy,
    )


def passes(tree: exp.Expr, policy: GovernancePolicy, model: Model | None = None) -> bool:
    try:
        verify(tree, plan_for(policy, model))
    except GovernanceError:
        return False
    return True


def named(expression: exp.Expr, alias: str) -> exp.Expr:
    return exp.alias_(expression, alias)


def count() -> exp.Expr:
    return exp.Count(this=exp.Star())


def case_in_case(column: str) -> exp.Expr:
    return exp.Case(
        ifs=[
            exp.If(this=exp.LT(this=col(column), expression=exp.Null()), true=exp.Literal.number(1))
        ]
    )


def eq(column: str) -> exp.Expr:
    return exp.EQ(this=col(column), expression=exp.Literal.string("x"))


def where(column: str) -> exp.Select:
    return select(count()).where(eq(column))


def other_table() -> exp.Table:
    return exp.table_("contracts", db="core", catalog="lending", alias=exp.to_identifier("k"))


def inner_email() -> exp.Select:
    return select(named(col("email", "s"), "customer__email"), table=customers("s"))


FULL = "customer__full_name"
BIRTH = "customer__birth_year"
CASES: dict[str, tuple[Callable[[], exp.Expr], frozenset[str]]] = {
    # ---- the select list and GROUP BY: a whole item under the alias of a dimension the plan
    # ---- selected raw, and a rule that lets that dimension be returned raw
    "select a column of a dimension with no mask, under its alias": (
        lambda: select(named(col("full_name"), FULL)),
        NO_MASK_SELECT,
    ),
    "select a column of a dimension with a mask, under its alias": (
        lambda: select(named(col("birth_date"), BIRTH)),
        MASKED_SELECT,
    ),
    "select the raw column bare": (lambda: select(col("full_name")), NONE),
    "select the raw column under another alias": (
        lambda: select(named(col("full_name"), "something_else")),
        NONE,
    ),
    "select a column of one dimension under the alias of another": (
        lambda: select(named(col("email"), FULL)),
        NO_MASK_SELECT,  # the alias is a raw dimension; the column check is per column
    ),
    "group by the column of a dimension with no mask": (
        lambda: select(named(col("national_id"), "customer__national_id"), count()).group_by(
            col("national_id")
        ),
        NO_MASK_SELECT,
    ),
    "group by a column that no select item shows": (
        lambda: select(count()).group_by(col("birth_date")),
        NONE,
    ),
    "an expression over a raw column under its alias": (
        lambda: select(named(exp.Upper(this=col("full_name")), FULL)),
        NO_MASK_SELECT,
    ),
    "a date truncation of a raw column under the alias of a masked dimension": (
        lambda: select(
            named(exp.TimestampTrunc(this=col("birth_date"), unit=exp.Literal.string("day")), BIRTH)
        ),
        MASKED_SELECT,
    ),
    "a raw column in a CTE under its alias": (
        lambda: (
            exp.select(exp.column(FULL))
            .from_("m")
            .with_("m", as_=select(named(col("full_name"), FULL)))
        ),
        NO_MASK_SELECT,
    ),
    "a raw column in a scalar subquery of a select item": (
        lambda: select(named(inner_email().subquery(), "customer__email")),
        NONE,
    ),
    "a raw column in a subquery that is joined": (
        lambda: select(exp.Literal.number(1)).join(
            exp.Join(this=inner_email().subquery("x"), on=exp.true(), side="LEFT")
        ),
        NONE,
    ),
    "a raw column in a correlated subquery of a select item": (
        lambda: select(
            named(
                exp.select(count())
                .from_(customers("s"))
                .where(exp.LT(this=col("full_name", "s"), expression=col("full_name")))
                .subquery(),
                "customer__rank",
            )
        ),
        NONE,
    ),
    # ---- WHERE: needs allow_personal_filters, in a query of its own
    "filter on a column of a dimension with no mask": (lambda: where("full_name"), WHERE),
    "filter on a column of a dimension with a mask": (lambda: where("birth_date"), WHERE),
    "filter on a masked column": (
        lambda: select(count()).where(
            exp.EQ(this=mask_expression(YEAR, col("birth_date")), expression=exp.Null())
        ),
        WHERE,
    ),
    "a month-end selection with MAX in a subquery of the filter": (
        lambda: select(count()).where(
            exp.In(
                this=col("birth_date"),
                query=select(exp.Max(this=col("birth_date")))
                .group_by(col("birth_date"))
                .subquery(),
            )
        ),
        WHERE,
    ),
    "a filter inside a subquery of a select item": (
        lambda: select(
            named(
                select(count())
                .where(
                    eq(
                        "full_name",
                    )
                )
                .subquery(),
                "customer__n",
            )
        ),
        NONE,
    ),
    # ---- never allowed
    "MIN of a personal column": (lambda: select(exp.Min(this=col("full_name"))), NONE),
    "MIN of a personal column under a raw alias": (
        lambda: select(named(exp.Min(this=col("full_name")), FULL)),
        NONE,
    ),
    "MAX of a column of a dimension with a mask": (
        lambda: select(exp.Max(this=col("birth_date"))),
        NONE,
    ),
    "SUM of a personal column": (lambda: select(exp.Sum(this=col("birth_date"))), NONE),
    "AVG of a personal column": (lambda: select(exp.Avg(this=col("full_name"))), NONE),
    "MAX of a personal column with a group by": (
        lambda: select(exp.Max(this=col("email"))).group_by(col("email")),
        NONE,
    ),
    "COUNT of an expression over a personal column": (
        lambda: select(exp.Count(this=case_in_case("birth_date"))),
        NONE,
    ),
    "a window over a personal column": (
        lambda: select(
            exp.Window(
                this=exp.RowNumber(),
                order=exp.Order(expressions=[exp.Ordered(this=col("full_name"))]),
            )
        ),
        NONE,
    ),
    "JSONB_AGG of a personal column": (
        lambda: select(exp.Anonymous(this="jsonb_agg", expressions=[col("email")])),
        NONE,
    ),
    "set_config with a personal column": (
        lambda: select(count()).where(
            exp.Anonymous(
                this="set_config", expressions=[exp.Literal.string("k"), col("full_name")]
            )
        ),
        NONE,
    ),
    "set_config with no personal column": (
        lambda: exp.select(exp.Anonymous(this="set_config", expressions=[exp.Literal.string("k")])),
        NONE,
    ),
    "current_setting with no personal column": (
        lambda: exp.select(
            exp.Anonymous(this="current_setting", expressions=[exp.Literal.string("k")])
        ),
        NONE,
    ),
    "HAVING on a personal column": (
        lambda: select(count()).group_by(col("province")).having(eq("full_name")),
        NONE,
    ),
    "ORDER BY a personal column": (
        lambda: select(exp.Literal.number(1)).order_by(exp.Ordered(this=col("full_name"))),
        NONE,
    ),
    "a personal column in a join condition": (
        lambda: select(exp.Literal.number(1)).join(
            exp.Join(
                this=other_table(),
                on=exp.EQ(this=col("national_id"), expression=col("customer_id", "k")),
                side="LEFT",
            )
        ),
        NONE,
    ),
    "SELECT *": (lambda: select(exp.Star()), NONE),
    "t.*": (lambda: select(exp.Column(this=exp.Star(), table=exp.to_identifier("c"))), NONE),
    # ---- counts, ordinary columns, and allowed functions pass under every policy
    "COUNT of a personal column": (lambda: select(exp.Count(this=col("email"))), ALL),
    "COUNT DISTINCT of a personal column": (
        lambda: select(exp.Count(this=exp.Distinct(expressions=[col("national_id")]))),
        ALL,
    ),
    "an ordinary column": (lambda: select(col("province"), col("city")), ALL),
    "an ordinary column through an allowed function": (
        lambda: select(exp.Lower(this=col("province"))),
        ALL,
    ),
    "the same name on another table": (
        lambda: exp.select(col("email", "k")).from_(other_table()),
        ALL,
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("policy_name", sorted(POLICIES))
def test_verify_accepts_exactly_what_the_policy_allows(name: str, policy_name: str) -> None:
    build, accepted = CASES[name]
    assert passes(build(), POLICIES[policy_name]) == (policy_name in accepted)


def test_off_is_checked_exactly_as_custom_with_every_rule_relaxed() -> None:
    for name, (build, _) in CASES.items():
        assert passes(build(), POLICIES["off"]) == passes(build(), POLICIES["all_custom"]), name


def test_the_matrix_covers_the_cases_of_the_brief() -> None:
    """With only `allow_personal_filters`, a raw personal column in the select list still fails;
    MIN, MAX, SUM, and AVG fail under every policy, `off` included."""
    assert not passes(select(named(col("full_name"), FULL)), POLICIES["filters"])
    assert not passes(select(named(col("birth_date"), BIRTH)), POLICIES["filters"])
    for aggregate in (exp.Min, exp.Max, exp.Sum, exp.Avg):
        for column in ("full_name", "birth_date"):
            for policy in POLICIES.values():
                assert not passes(select(aggregate(this=col(column))), policy)


def test_the_wp10_measure_attacks_on_a_catalog_only_personal_column_fail_under_off() -> None:
    """A column only the catalog tags personal, with no dimension: a measure may count it and
    nothing else may read it. `off` does not change that."""
    model = with_derived(lending(), {"lending.core.customers.customer_id": "personal"})
    attacks = [
        select(exp.Max(this=col("customer_id"))),
        select(exp.Count(this=case_in_case("customer_id"))),
        select(exp.Sum(this=col("customer_id"))),
        select(named(col("customer_id"), "customer__id")),
    ]
    for policy in (POLICIES["off"], POLICIES["all_custom"], STRICT):
        for tree in attacks:
            assert not passes(tree, policy, model)
        assert passes(select(exp.Count(this=col("customer_id"))), policy, model)
    assert passes(where("customer_id"), POLICIES["off"], model)
    assert not passes(where("customer_id"), STRICT, model)


def test_a_mask_shaped_expression_the_plan_did_not_build_still_fails_under_custom() -> None:
    mask = mask_expression(YEAR, col("birth_date"))
    for name in ("strict", "filters", "unmasked", "no_minimum"):
        assert not passes(select(mask.as_("something_else")), POLICIES[name])


def test_the_planned_mask_still_passes_under_every_policy_that_applies_masks() -> None:
    mask = mask_expression(YEAR, col("birth_date"))
    tree = select(mask.as_(BIRTH), exp.Count(this=exp.Star())).group_by(mask.copy())
    for name in ("strict", "filters", "unmasked", "no_minimum"):
        verify(tree, plan_for(POLICIES[name]))


def test_a_column_one_masked_dimension_reads_is_not_raw_for_an_unmasked_dimension_rule() -> None:
    """`birth_date` is read by a dimension with a mask and by a new one with none. Returning
    dimensions with no mask raw does not let `birth_date` out, because one reader may not be
    returned raw. With both rules relaxed it may."""
    extra = Dimension(
        name="birth_day",
        type="time",
        expr="birth_date",
        description="The birth date, without a mask.",
        sensitivity="personal",
    )
    entity = lending().entities["customer"]
    updated = entity.model_copy(update={"dimensions": [*entity.dimensions, extra]})
    model = Model.build(
        [updated, *[e for e in lending().entities.values() if e.name != "customer"]],
        lending().metrics.values(),
    )
    tree = select(named(col("birth_date"), "customer__birth_day"))

    def plan(policy: GovernancePolicy) -> QueryPlan:
        return plan_request(
            model,
            request("new_customer_count", dimensions=["customer.birth_day"]),
            governance=policy,
        )

    for name, accepted in (("unmasked", False), ("raw_masks", False), ("all_custom", True)):
        try:
            verify(tree, plan(POLICIES[name]))
            ok = True
        except (GovernanceError, QueryError):  # the planner refuses the dimension itself
            ok = False
        assert ok == accepted, name


def test_a_grain_released_by_the_policy_is_accepted_as_metric_time_only() -> None:
    model = with_derived(lending(), {"lending.core.payments.posting_date": "personal"})
    policy = GovernancePolicy.custom(allow_unmasked_personal=True, min_group_size=0)
    the_plan = plan_request(model, request("collected_amount", grain="month"), governance=policy)
    tree = build_tree(model, the_plan)
    verify(tree, the_plan)

    def trunc(alias: str) -> exp.Select:
        payments = exp.table_("payments", db="core", catalog="lending", alias="p")
        day = exp.TimestampTrunc(this=col("posting_date", "p"), unit=exp.Literal.string("month"))
        return exp.select(exp.alias_(day, alias)).from_(payments)

    verify(trunc("metric_time"), the_plan)
    with pytest.raises(GovernanceError):
        verify(trunc("something_else"), the_plan)
    ungrained = plan_request(model, request("collected_amount"), governance=policy)
    with pytest.raises(GovernanceError):
        verify(trunc("metric_time"), ungrained)


def test_the_plan_carries_the_policy_and_the_raw_columns_the_policy_allows() -> None:
    assert plan_for(STRICT).raw_columns == frozenset()
    assert plan_for(POLICIES["filters"]).raw_columns == frozenset()
    assert plan_for(POLICIES["no_minimum"]).raw_columns == frozenset()
    customers_table = "lending.core.customers"
    assert plan_for(POLICIES["unmasked"]).raw_columns == {
        (customers_table, c) for c in ("full_name", "national_id", "email", "phone_number")
    }
    assert plan_for(POLICIES["raw_masks"]).raw_columns == {(customers_table, "birth_date")}
    assert plan_for(POLICIES["all_custom"]).raw_columns == {
        (customers_table, c)
        for c in ("full_name", "national_id", "email", "phone_number", "birth_date")
    }
    assert plan_for(POLICIES["off"]).governance == GovernancePolicy.off()


def test_the_function_check_runs_under_every_policy() -> None:
    from makna.model.functions import ALLOWED_ANONYMOUS

    assert "set_config" not in ALLOWED_ANONYMOUS and "current_setting" not in ALLOWED_ANONYMOUS
    for policy in POLICIES.values():
        for name in ("set_config", "current_setting", "pg_read_file", "nextval", "random"):
            node = exp.Anonymous(this=name, expressions=[exp.Literal.string("k")])
            assert not passes(exp.select(node), policy), (name, policy.mode)
