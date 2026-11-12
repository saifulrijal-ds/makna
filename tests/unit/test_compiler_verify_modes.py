"""WP-10B criterion 4 (D-29): `governance.verify` follows the policy of the plan.

Each case is a hand-built tree over the sample `customers` table. For each policy the table says
whether `verify` passes it. `strict` must reject exactly what it rejected before the modes
(`test_compiler_verify.py` has the full WP-10 set); `custom` accepts a raw personal column only
where an enabled rule allows it; `off` checks nothing personal.

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
from makna.model.definitions import Dimension, MaskTruncateDate
from makna.model.governance import STRICT, GovernancePolicy

from .compiler_support import lending, request
from .test_compiler_refusals import with_derived
from .test_compiler_verify import col, select

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
ONLY_OFF = frozenset({"off"})
# Where a raw column of a dimension with no masking rule (full_name) may be returned.
NO_MASK_SELECT = frozenset({"unmasked", "all_custom", "off"})
# Where a raw column of a dimension with a masking rule (birth_date) may be returned.
MASKED_SELECT = frozenset({"raw_masks", "all_custom", "off"})
WHERE = frozenset({"filters", "all_custom", "off"})
YEAR = MaskTruncateDate(method="truncate_date", grain="year")


def plan_for(policy: GovernancePolicy, model: Model | None = None) -> QueryPlan:
    return plan_request(model or lending(), request("outstanding_principal"), governance=policy)


def passes(tree: exp.Expr, policy: GovernancePolicy, model: Model | None = None) -> bool:
    try:
        verify(tree, plan_for(policy, model))
    except GovernanceError:
        return False
    return True


def max_of(column: str) -> exp.Expr:
    return exp.Max(this=col(column))


def case_in_case(column: str) -> exp.Expr:
    return exp.Case(
        ifs=[
            exp.If(this=exp.LT(this=col(column), expression=exp.Null()), true=exp.Literal.number(1))
        ]
    )


def eq(column: str) -> exp.Expr:
    return exp.EQ(this=col(column), expression=exp.Literal.string("x"))


def where(column: str) -> exp.Select:
    return select(exp.Count(this=exp.Star())).where(eq(column))


def other_table() -> exp.Table:
    return exp.table_("contracts", db="core", catalog="lending", alias=exp.to_identifier("k"))


CASES: dict[str, tuple[Callable[[], exp.Expr], frozenset[str]]] = {
    # ---- the select list and GROUP BY: needs a rule for returning the dimension raw
    "select a column of a dimension with no mask": (
        lambda: select(col("full_name")),
        NO_MASK_SELECT,
    ),
    "select a column of a dimension with a mask": (
        lambda: select(col("birth_date")),
        MASKED_SELECT,
    ),
    "select under an alias": (lambda: select(col("email").as_("customer__email")), NO_MASK_SELECT),
    "group by a column of a dimension with no mask": (
        lambda: select(col("national_id"), exp.Count(this=exp.Star())).group_by(col("national_id")),
        NO_MASK_SELECT,
    ),
    "group by only, select count": (
        lambda: select(exp.Count(this=exp.Star())).group_by(col("birth_date")),
        MASKED_SELECT,
    ),
    "an expression over a raw column in the select list": (
        lambda: select(exp.Upper(this=col("full_name"))),
        NO_MASK_SELECT,
    ),
    "a date truncation of a raw column in the select list": (
        lambda: select(exp.TimestampTrunc(this=col("birth_date"), unit=exp.Literal.string("day"))),
        MASKED_SELECT,
    ),
    "select a raw column in a CTE": (
        lambda: (
            exp.select(exp.column("n")).from_("m").with_("m", as_=select(col("full_name").as_("n")))
        ),
        NO_MASK_SELECT,
    ),
    # ---- WHERE: needs allow_personal_filters
    "filter on a column of a dimension with no mask": (lambda: where("full_name"), WHERE),
    "filter on a column of a dimension with a mask": (lambda: where("birth_date"), WHERE),
    "filter on a masked column": (
        lambda: select(exp.Count(this=exp.Star())).where(
            exp.EQ(this=mask_expression(YEAR, col("birth_date")), expression=exp.Null())
        ),
        WHERE,
    ),
    "a month-end selection with MAX in a subquery of the filter": (
        lambda: select(exp.Count(this=exp.Star())).where(
            exp.In(
                this=col("birth_date"),
                query=select(exp.Max(this=col("birth_date")))
                .group_by(col("birth_date"))
                .subquery(),
            )
        ),
        WHERE,
    ),
    # ---- never allowed by a rule of custom (only `off` accepts them)
    "MIN of a personal column": (lambda: select(exp.Min(this=col("full_name"))), ONLY_OFF),
    "MAX of a column of a dimension with a mask": (lambda: select(max_of("birth_date")), ONLY_OFF),
    "SUM of a personal column": (lambda: select(exp.Sum(this=col("birth_date"))), ONLY_OFF),
    "AVG of a personal column": (lambda: select(exp.Avg(this=col("full_name"))), ONLY_OFF),
    "MAX of a personal column with a group by": (
        lambda: select(max_of("email")).group_by(col("email")),
        ONLY_OFF,
    ),
    "COUNT of an expression over a personal column": (
        lambda: select(exp.Count(this=case_in_case("birth_date"))),
        ONLY_OFF,
    ),
    "a window over a personal column": (
        lambda: select(
            exp.Window(
                this=exp.RowNumber(),
                order=exp.Order(expressions=[exp.Ordered(this=col("full_name"))]),
            )
        ),
        ONLY_OFF,
    ),
    "HAVING on a personal column": (
        lambda: (
            select(exp.Count(this=exp.Star())).group_by(col("province")).having(eq("full_name"))
        ),
        ONLY_OFF,
    ),
    "ORDER BY a personal column": (
        lambda: select(exp.Literal.number(1)).order_by(exp.Ordered(this=col("full_name"))),
        ONLY_OFF,
    ),
    "a personal column in a join condition": (
        lambda: select(exp.Literal.number(1)).join(
            exp.Join(
                this=other_table(),
                on=exp.EQ(this=col("national_id"), expression=col("customer_id", "k")),
                side="LEFT",
            )
        ),
        ONLY_OFF,
    ),
    "SELECT *": (lambda: select(exp.Star()), ONLY_OFF),
    "t.*": (lambda: select(exp.Column(this=exp.Star(), table=exp.to_identifier("c"))), ONLY_OFF),
    # ---- counts and ordinary columns pass under every policy
    "COUNT of a personal column": (lambda: select(exp.Count(this=col("email"))), ALL),
    "COUNT DISTINCT of a personal column": (
        lambda: select(exp.Count(this=exp.Distinct(expressions=[col("national_id")]))),
        ALL,
    ),
    "an ordinary column": (lambda: select(col("province"), col("city")), ALL),
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


def test_the_matrix_covers_the_cases_of_the_brief() -> None:
    """With only `allow_personal_filters`, a raw personal column in the select list still fails;
    MIN, MAX, SUM, and AVG fail under every custom policy."""
    assert not passes(select(col("full_name")), POLICIES["filters"])
    assert not passes(select(col("birth_date")), POLICIES["filters"])
    for aggregate in (exp.Min, exp.Max, exp.Sum, exp.Avg):
        for column in ("full_name", "birth_date"):
            for name, policy in POLICIES.items():
                assert passes(select(aggregate(this=col(column))), policy) == (name == "off")


def test_a_mask_shaped_expression_the_plan_did_not_build_still_fails_under_custom() -> None:
    mask = mask_expression(YEAR, col("birth_date"))
    for name in ("strict", "filters", "unmasked", "no_minimum"):
        assert not passes(select(mask.as_("customer__birth_year")), POLICIES[name])


def test_the_planned_mask_still_passes_under_every_policy_that_applies_masks() -> None:
    mask = mask_expression(YEAR, col("birth_date"))
    tree = select(mask.as_("customer__birth_year"), exp.Count(this=exp.Star())).group_by(
        mask.copy()
    )
    for name in ("strict", "filters", "unmasked", "no_minimum"):
        the_plan = plan_request(
            lending(),
            request("outstanding_principal", dimensions=["customer.birth_year"]),
            governance=POLICIES[name],
        )
        verify(tree, the_plan)


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
    tree = select(col("birth_date"))
    assert not passes(tree, POLICIES["unmasked"], model)
    assert not passes(tree, POLICIES["raw_masks"], model)
    assert passes(tree, POLICIES["all_custom"], model)


def test_a_catalog_personal_column_no_dimension_reads_is_never_raw_in_the_select_list() -> None:
    model = with_derived(lending(), {"lending.core.customers.customer_id": "personal"})
    for name in ("unmasked", "raw_masks", "all_custom"):
        assert not passes(select(col("customer_id")), POLICIES[name], model)
    assert passes(select(col("customer_id")), POLICIES["off"], model)
    assert passes(where("customer_id"), POLICIES["all_custom"], model)
    assert not passes(where("customer_id"), POLICIES["unmasked"], model)


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


def test_off_does_not_skip_the_trees_that_hold_no_personal_column() -> None:
    """`verify` under `off` returns early only for personal columns; a hand-built tree with a
    personal column in a join condition passes, which `strict` rejects."""
    on = exp.EQ(this=col("national_id"), expression=col("customer_id", "k"))
    tree = select(exp.Literal.number(1)).join(exp.Join(this=other_table(), on=on, side="LEFT"))
    assert passes(tree, POLICIES["off"])
    assert not passes(tree, POLICIES["strict"])
