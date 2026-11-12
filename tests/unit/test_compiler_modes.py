"""WP-10B criteria 2, 3 and 6 (D-29): what each governance mode and each custom rule changes.

Every `custom` test relaxes one rule and checks two things: that rule's behaviour changed, and
that the other three rules still hold. The sample customer holds four personal dimensions with no
masking rule (`full_name`, `national_id`, `email`, `phone_number`) and one with a rule
(`birth_year`, `truncate_date` to the year). The personal official dates come from a model in
which the catalog tags the snapshot date of the balance table and the disbursement date personal.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from makna.compiler.compile import compile_request
from makna.compiler.governance import GROUPS_LEFT_OUT, GovernanceError, verify
from makna.compiler.plan import plan_request
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import build_tree
from makna.model.definitions import MaskTruncateDate
from makna.model.governance import STRICT, GovernancePolicy
from makna.model.request import CompileResponse, ErrorCode, QueryError

from .compiler_support import lending, request
from .duckdb_support import run_tree
from .test_compiler_masking import dimension_of, masked
from .test_compiler_refusals import with_derived

SNAPSHOTS = "lending.core.contract_monthly_snapshots"
PAYMENTS = "lending.core.payments"
RAW_NAME = '"t0"."full_name"'
RAW_BIRTH = '"t0"."birth_date"'
PERSONAL_FILTER = {"dimension": "customer.email", "op": "=", "value": "a@example.com"}

OFF = GovernancePolicy.off()
ONLY_MINIMUM = GovernancePolicy.custom(min_group_size=0)
ONLY_FILTERS = GovernancePolicy.custom(allow_personal_filters=True)
ONLY_UNMASKED = GovernancePolicy.custom(allow_unmasked_personal=True)
ONLY_RAW_MASKED = GovernancePolicy.custom(apply_masks=False)


def personal_dates() -> Model:
    """The snapshot date of the balance table and the disbursement date are personal."""
    return with_derived(
        lending(),
        {f"{SNAPSHOTS}.snapshot_date": "personal", f"{PAYMENTS}.posting_date": "personal"},
    )


def masked_dates() -> Model:
    """Like `personal_dates`, and the disbursement date has a masking rule."""
    month = MaskTruncateDate(method="truncate_date", grain="month")
    model = masked(
        personal_dates(),
        "payment.posting_date",
        dimension_of(personal_dates(), "payment.posting_date", masking=month),
    )
    return model


def compile_with(
    policy: GovernancePolicy | None, metric: str, model: Model | None = None, **fields: Any
) -> CompileResponse:
    chosen = model or lending()
    if policy is None:
        return compile_request(chosen, request(metric, **fields))
    return compile_request(chosen, request(metric, **fields), governance=policy)


def refusal(
    policy: GovernancePolicy, metric: str, model: Model | None = None, **fields: Any
) -> QueryError:
    with pytest.raises(QueryError) as caught:
        compile_with(policy, metric, model, **fields)
    assert caught.value.code == ErrorCode.PERSONAL_DATA_REFUSED
    return caught.value


# ---- criterion 2: `off` ------------------------------------------------------------------


def test_off_returns_full_name_raw(lending_db: duckdb.DuckDBPyConnection) -> None:
    fields: dict[str, Any] = {"dimensions": ["customer.full_name"]}
    response = compile_with(OFF, "new_customer_count", **fields)
    assert RAW_NAME in response.sql
    plan = plan_request(lending(), request("new_customer_count", **fields), governance=OFF)
    rows = run_tree(lending_db, build_tree(lending(), plan))
    names = {name for (name,) in lending_db.execute("SELECT full_name FROM customers").fetchall()}
    assert {row[0] for row in rows} <= names
    assert len(rows) > 50  # one row per name, not a mask and not a suppressed list
    assert [c.name for c in response.columns] == ["customer__full_name", "new_customer_count"]


def test_off_plans_a_filter_on_a_personal_dimension(lending_db: duckdb.DuckDBPyConnection) -> None:
    response = compile_with(OFF, "new_customer_count", filters=[PERSONAL_FILTER])
    assert '"t0"."email" = \'a@example.com\'' in response.sql
    plan = plan_request(
        lending(), request("new_customer_count", filters=[PERSONAL_FILTER]), governance=OFF
    )
    assert run_tree(lending_db, build_tree(lending(), plan)) == [(0,)]


def test_off_adds_no_record_count_and_no_groups_left_out_column() -> None:
    cases: list[dict[str, Any]] = [
        {"dimensions": ["customer.full_name"]},
        {"dimensions": ["customer.birth_year", "customer.province"]},
    ]
    for fields in cases:
        for metric in ("new_customer_count", "outstanding_principal", "npf_ratio"):
            response = compile_with(OFF, metric, **fields)
            assert GROUPS_LEFT_OUT not in [c.name for c in response.columns]
            assert GROUPS_LEFT_OUT not in response.sql
            assert "__records_" not in response.sql
            assert not any("fewer than" in w for w in response.warnings)


def test_off_passes_verify_and_returns_the_raw_birth_date() -> None:
    fields: dict[str, Any] = {"dimensions": ["customer.birth_year", "customer.national_id"]}
    plan = plan_request(lending(), request("new_customer_count", **fields), governance=OFF)
    tree = build_tree(lending(), plan)
    verify(tree, plan)
    sql = compile_with(OFF, "new_customer_count", **fields).sql
    assert RAW_BIRTH in sql
    assert "DATE_TRUNC" not in sql


def test_off_warning_names_the_mode_and_every_rule() -> None:
    [warning] = compile_with(OFF, "new_customer_count").warnings
    assert "mode off" in warning
    for rule in (
        "min_group_size = 0",
        "allow_personal_filters = true",
        "apply_masks = false",
        "allow_unmasked_personal = true",
    ):
        assert rule in warning, (rule, warning)


def test_off_still_runs_the_structural_checks_of_the_planner() -> None:
    """A metric named like a dimension column would give two result columns one name; the
    planner refuses that in every mode."""
    clash = (
        lending().metrics["new_customer_count"].model_copy(update={"name": "customer__full_name"})
    )
    model = Model.build(lending().entities.values(), [*lending().metrics.values(), clash])
    with pytest.raises(GovernanceError, match="customer__full_name"):
        plan_request(
            model, request("customer__full_name", dimensions=["customer.full_name"]), governance=OFF
        )


# ---- criterion 3: each custom rule alone ------------------------------------------------


def assert_strict_rules_hold(policy: GovernancePolicy, *, except_rule: str) -> None:
    """Every rule except `except_rule` still behaves as under `strict`."""
    if except_rule != "allow_unmasked_personal":
        error = refusal(policy, "new_customer_count", dimensions=["customer.full_name"])
        assert error.details["use"] == "group"
    if except_rule != "allow_personal_filters":
        error = refusal(policy, "new_customer_count", filters=[PERSONAL_FILTER])
        assert error.details["use"] == "filter"
        refusal(policy, "collected_amount", personal_dates(), time_start="2026-01-01")
    if except_rule != "apply_masks":
        sql = compile_with(policy, "new_customer_count", dimensions=["customer.birth_year"]).sql
        assert f"DATE_TRUNC('YEAR', {RAW_BIRTH})" in sql  # the mask, not the raw column
        assert f"{RAW_BIRTH} AS" not in sql
        assert 'AS DATE) AS "customer__birth_year"' in sql
    if except_rule != "min_group_size":
        response = compile_with(policy, "new_customer_count", dimensions=["customer.birth_year"])
        assert GROUPS_LEFT_OUT in [c.name for c in response.columns]
        assert f">= {policy.min_group_size}" in response.sql
        assert any("fewer than" in w for w in response.warnings)


def test_only_the_minimum_off_changes_only_the_minimum() -> None:
    assert_strict_rules_hold(ONLY_MINIMUM, except_rule="min_group_size")
    for dims in (["customer.birth_year"], ["customer.birth_year", "branch.region_name"]):
        strict = compile_with(STRICT, "outstanding_principal", dimensions=dims)
        relaxed = compile_with(ONLY_MINIMUM, "outstanding_principal", dimensions=dims)
        assert GROUPS_LEFT_OUT in [c.name for c in strict.columns]
        assert GROUPS_LEFT_OUT not in [c.name for c in relaxed.columns]
        assert "__records_" not in relaxed.sql
        assert "DATE_TRUNC('YEAR'" in relaxed.sql  # the mask is still there


def test_only_personal_filters_allowed_changes_only_filters() -> None:
    assert_strict_rules_hold(ONLY_FILTERS, except_rule="allow_personal_filters")
    response = compile_with(ONLY_FILTERS, "new_customer_count", filters=[PERSONAL_FILTER])
    assert '"t0"."email" = \'a@example.com\'' in response.sql
    # a time range on a personal official date is a filter
    ranged = compile_with(
        ONLY_FILTERS, "collected_amount", personal_dates(), time_start="2026-01-01"
    )
    assert '"t0"."posting_date" >= CAST(\'2026-01-01\' AS DATE)' in ranged.sql
    # so is a balance's month-end selection on a personal snapshot date
    balance = compile_with(ONLY_FILTERS, "outstanding_principal", personal_dates())
    assert '"t0"."snapshot_date" IN (' in balance.sql
    # but a grain selects the raw date, which this rule does not allow
    error = refusal(ONLY_FILTERS, "collected_amount", personal_dates(), grain="month")
    assert error.details["use"] == "time"


def test_only_unmasked_personal_allowed_changes_only_unmasked_selection() -> None:
    assert_strict_rules_hold(ONLY_UNMASKED, except_rule="allow_unmasked_personal")
    for ref, column in (
        ("customer.full_name", "full_name"),
        ("customer.national_id", "national_id"),
        ("customer.email", "email"),
        ("customer.phone_number", "phone_number"),
    ):
        response = compile_with(ONLY_UNMASKED, "new_customer_count", dimensions=[ref])
        assert f'"t0"."{column}" AS' in response.sql
    # a grain on a personal date that has no masking rule selects the raw date: allowed
    grain = compile_with(ONLY_UNMASKED, "collected_amount", personal_dates(), grain="month")
    assert 'DATE_TRUNC(\'MONTH\', "t0"."posting_date")' in grain.sql
    # the date is still a filter when it has a time range, and a balance still filters it
    refusal(ONLY_UNMASKED, "collected_amount", personal_dates(), time_end="2026-07-01")
    refusal(ONLY_UNMASKED, "outstanding_principal", personal_dates())
    # a dimension that has a masking rule is still masked
    assert (
        "DATE_TRUNC('YEAR'"
        in compile_with(ONLY_UNMASKED, "new_customer_count", dimensions=["customer.birth_year"]).sql
    )
    # and a personal date that has a masking rule is not returned raw by this rule
    refusal(ONLY_UNMASKED, "collected_amount", masked_dates(), grain="month")


def test_only_masks_off_changes_only_masked_dimensions() -> None:
    assert_strict_rules_hold(ONLY_RAW_MASKED, except_rule="apply_masks")
    response = compile_with(
        ONLY_RAW_MASKED, "new_customer_count", dimensions=["customer.birth_year"]
    )
    assert f'{RAW_BIRTH} AS "customer__birth_year"' in response.sql
    assert "DATE_TRUNC" not in response.sql
    assert GROUPS_LEFT_OUT in [c.name for c in response.columns]  # the minimum still applies
    # a personal date that has a masking rule is returned raw by a grain; one without is not
    grain = compile_with(ONLY_RAW_MASKED, "collected_amount", masked_dates(), grain="month")
    assert 'DATE_TRUNC(\'MONTH\', "t0"."posting_date")' in grain.sql
    refusal(ONLY_RAW_MASKED, "collected_amount", personal_dates(), grain="month")
    refusal(ONLY_RAW_MASKED, "collected_amount", masked_dates(), time_start="2026-01-01")


def test_a_balance_with_a_personal_snapshot_date_and_a_grain_needs_both_rules() -> None:
    both = GovernancePolicy.custom(
        allow_personal_filters=True, allow_unmasked_personal=True, min_group_size=0
    )
    response = compile_with(
        both, "outstanding_principal", personal_dates(), grain="month", time_start="2026-01-01"
    )
    assert "snapshot_date" in response.sql
    refusal(ONLY_FILTERS, "outstanding_principal", personal_dates(), grain="month")
    refusal(ONLY_UNMASKED, "outstanding_principal", personal_dates(), grain="month")


def test_the_minimum_cannot_be_applied_to_a_grain_on_a_date_that_is_part_of_the_key() -> None:
    """The balance table is keyed by (snapshot_date, contract_id). With the snapshot date
    personal, counting its records would read it, so a grain on it is refused while the minimum
    is on; it is allowed only when the adopter also turns the minimum off."""
    both = GovernancePolicy.custom(allow_personal_filters=True, allow_unmasked_personal=True)
    error = refusal(both, "outstanding_principal", personal_dates(), grain="month")
    assert "minimum group size" in error.message


def test_a_minimum_other_than_zero_in_custom_keeps_the_rule() -> None:
    response = compile_with(
        GovernancePolicy.custom(min_group_size=3),
        "new_customer_count",
        dimensions=["customer.birth_year"],
    )
    assert ">= 3" in response.sql
    assert GROUPS_LEFT_OUT in [c.name for c in response.columns]


def test_the_minimum_still_counts_records_when_a_personal_dimension_is_returned_raw() -> None:
    response = compile_with(ONLY_UNMASKED, "new_customer_count", dimensions=["customer.full_name"])
    assert GROUPS_LEFT_OUT in [c.name for c in response.columns]
    assert "COUNT(DISTINCT" in response.sql and ">= 10" in response.sql


# ---- criterion 6: the warning and the explanation ----------------------------------------


def test_strict_and_an_untouched_custom_add_no_warning_and_change_nothing() -> None:
    for metric, fields in (
        ("new_customer_count", {}),
        ("new_customer_count", {"dimensions": ["customer.birth_year"]}),
        ("outstanding_principal", {"grain": "month", "dimensions": ["branch.region_name"]}),
    ):
        default = compile_with(None, metric, **fields)
        assert compile_with(STRICT, metric, **fields) == default
        assert compile_with(GovernancePolicy.custom(), metric, **fields) == default
        assert not any("Governance" in w for w in default.warnings)
        assert not any("governance" in s for s in default.explanation)
    five = compile_with(
        GovernancePolicy.strict(5), "new_customer_count", dimensions=["customer.birth_year"]
    )
    assert (
        compile_with(
            GovernancePolicy.custom(min_group_size=5),
            "new_customer_count",
            dimensions=["customer.birth_year"],
        )
        == five
    )


@pytest.mark.parametrize(
    ("policy", "rules"),
    [
        (ONLY_MINIMUM, ["min_group_size = 0"]),
        (ONLY_FILTERS, ["allow_personal_filters = true"]),
        (ONLY_UNMASKED, ["allow_unmasked_personal = true"]),
        (ONLY_RAW_MASKED, ["apply_masks = false"]),
        (
            GovernancePolicy.custom(allow_personal_filters=True, apply_masks=False),
            ["allow_personal_filters = true", "apply_masks = false"],
        ),
    ],
)
def test_a_relaxed_custom_policy_adds_one_warning_that_names_the_mode_and_its_rules(
    policy: GovernancePolicy, rules: list[str]
) -> None:
    unrelaxed = [
        "min_group_size",
        "allow_personal_filters",
        "apply_masks",
        "allow_unmasked_personal",
    ]
    shapes: list[dict[str, Any]] = [{}, {"dimensions": ["customer.birth_year"]}]
    for fields in shapes:
        response = compile_with(policy, "new_customer_count", **fields)
        governance_warnings = [w for w in response.warnings if w.startswith("Governance mode")]
        assert len(governance_warnings) == 1
        [warning] = governance_warnings
        assert "mode custom" in warning
        assert all(rule in warning for rule in rules)
        for name in unrelaxed:
            assert (name in warning) == any(r.startswith(name) for r in rules), (name, warning)
        sentences = [s for s in response.explanation if "governance mode" in s]
        assert len(sentences) == 1
        assert "custom" in sentences[0]
        assert all(rule in sentences[0] for rule in rules)


def test_the_explanation_sentence_comes_after_the_mask_sentences_and_before_the_warnings() -> None:
    response = compile_with(
        ONLY_FILTERS,
        "new_customer_count",
        dimensions=["customer.birth_year"],
        filters=[PERSONAL_FILTER],
    )
    last = response.explanation[-1]
    assert "governance mode custom" in last
    assert "masked" in response.explanation[-2]


def test_the_policy_adds_no_sentence_or_warning_beyond_its_own() -> None:
    """Relaxing nothing that a request uses still reports the mode (the request may differ next
    time), and changes nothing else in the response."""
    plain = compile_with(STRICT, "collected_amount", grain="month")
    off = compile_with(OFF, "collected_amount", grain="month")
    assert off.sql == plain.sql
    assert off.columns == plain.columns
    assert off.explanation[:-1] == plain.explanation
    assert off.warnings[:-1] == plain.warnings
    assert len(off.warnings) == len(plain.warnings) + 1


# ---- the available dimensions follow the policy ---------------------------------------


def test_available_dimensions_list_unmasked_personal_ones_only_when_the_policy_allows() -> None:
    from makna.compiler.plan import available_dimensions

    metric = lending().metrics["new_customer_count"]
    assert "customer.full_name" not in available_dimensions(lending(), metric).refs()
    assert (
        "customer.full_name"
        not in available_dimensions(lending(), metric, governance=ONLY_FILTERS).refs()
    )
    assert (
        "customer.full_name"
        in available_dimensions(lending(), metric, governance=ONLY_UNMASKED).refs()
    )
    assert "customer.full_name" in available_dimensions(lending(), metric, governance=OFF).refs()


# ---- fix round 1: a grain on a personal official date is guarded like a dimension ------------


def registered_personal() -> Model:
    """The registration time of customers is personal (catalog tag): the date dimension of
    `new_customer_count` reads it."""
    return with_derived(lending(), {"lending.core.customers.registered_at": "personal"})


def test_a_grain_on_a_personal_date_leaves_out_small_groups(
    lending_full_db: duckdb.DuckDBPyConnection,
) -> None:
    fields: dict[str, Any] = {"grain": "day", "dimensions": ["customer.city", "customer.gender"]}
    model = registered_personal()
    plan = plan_request(
        model, request("new_customer_count", limit=10000, **fields), governance=ONLY_UNMASKED
    )
    assert [g.entity for g in plan.guard] == ["customer"]
    assert [c.name for c in plan.columns][-1] == GROUPS_LEFT_OUT
    rows = run_tree(lending_full_db, build_tree(model, plan))
    assert rows, "some groups of at least ten customers remain"
    assert all(row[3] >= 10 for row in rows)  # new_customer_count counts the customers
    assert rows[0][-1] > 1000  # the groups of one or two customers that were left out
    response = compile_with(ONLY_UNMASKED, "new_customer_count", model, **fields)
    assert any("fewer than 10 Customer records" in w for w in response.warnings)


def test_a_grain_on_a_personal_date_with_the_minimum_off_adds_no_guard() -> None:
    policy = GovernancePolicy.custom(allow_unmasked_personal=True, min_group_size=0)
    response = compile_with(
        policy,
        "new_customer_count",
        registered_personal(),
        grain="day",
        dimensions=["customer.city"],
    )
    assert GROUPS_LEFT_OUT not in [c.name for c in response.columns]
    assert "__records_" not in response.sql


def test_a_grain_on_a_masked_personal_date_is_guarded_when_masks_are_off() -> None:
    month = MaskTruncateDate(method="truncate_date", grain="month")
    model = masked(
        registered_personal(),
        "customer.registered_date",
        dimension_of(registered_personal(), "customer.registered_date", masking=month),
    )
    response = compile_with(ONLY_RAW_MASKED, "new_customer_count", model, grain="day")
    assert GROUPS_LEFT_OUT in [c.name for c in response.columns]
    assert ">= 10" in response.sql


def test_a_grain_on_a_personal_date_is_refused_by_strict_as_before() -> None:
    error = refusal(STRICT, "new_customer_count", registered_personal(), grain="day")
    assert error.details["use"] == "time"


# ---- fix round 1: wording ---------------------------------------------------------------------

STRICT_FILTER = (
    "customer.email is personal data and cannot be used in a filter, masked or not, because a "
    "filter on it would single out people. Remove the filter or filter on a dimension that is "
    "not personal."
)
STRICT_GROUP = (
    "customer.full_name is personal data and has no masked form, so it cannot be returned. "
    "Choose a dimension that is not personal."
)
STRICT_DATE = (
    "The official date of collected_amount (payment.posting_date) is personal data, so this "
    "metric cannot be grouped by time or limited to a time range. Ask without grain, "
    "time_start, and time_end, or choose another metric."
)


def test_strict_refusal_messages_are_unchanged() -> None:
    assert refusal(STRICT, "new_customer_count", filters=[PERSONAL_FILTER]).message == STRICT_FILTER
    group = refusal(STRICT, "new_customer_count", dimensions=["customer.full_name"])
    assert group.message == STRICT_GROUP
    date = refusal(STRICT, "collected_amount", personal_dates(), grain="month")
    assert date.message == STRICT_DATE


def test_a_refusal_under_another_policy_names_the_rule_that_would_allow_it() -> None:
    filtered = refusal(ONLY_UNMASKED, "new_customer_count", filters=[PERSONAL_FILTER])
    assert "allow_personal_filters" in filtered.message
    assert "governance mode of this server is custom" in filtered.message
    grouped = refusal(ONLY_FILTERS, "new_customer_count", dimensions=["customer.full_name"])
    assert "allow_unmasked_personal" in grouped.message
    # only a grain is refused here, so the message must not say a time range is refused
    grain = refusal(ONLY_FILTERS, "collected_amount", personal_dates(), grain="month")
    assert "allow_unmasked_personal" in grain.message
    assert "time range" not in grain.message.replace("a time range, or", "")
    ranged = refusal(ONLY_UNMASKED, "collected_amount", personal_dates(), time_start="2026-01-01")
    assert "allow_personal_filters" in ranged.message and "grain" not in ranged.message
    masked_grain = refusal(ONLY_UNMASKED, "collected_amount", masked_dates(), grain="month")
    assert "apply_masks = false" in masked_grain.message


def test_the_warning_and_the_sentence_say_filters_ignore_the_minimum_group_size() -> None:
    response = compile_with(ONLY_FILTERS, "new_customer_count", filters=[PERSONAL_FILTER])
    [warning] = [w for w in response.warnings if w.startswith("Governance mode")]
    sentence = next(s for s in response.explanation if "governance mode" in s)
    for text in (warning, sentence):
        assert "minimum group size does not apply to filters" in text
        assert "one person's records" in text
