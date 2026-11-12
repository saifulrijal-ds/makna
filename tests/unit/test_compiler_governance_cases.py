"""WP-10 fix round 1: cases found by the task and safety reviews.

Each test builds a model from the sample lending definitions with one change, as a curator or the
catalog could make it, and checks that the compiler refuses or fails closed.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path
from typing import Any

import duckdb
import pytest

from makna.cli.main import main
from makna.compiler.compile import compile_request
from makna.compiler.governance import GROUPS_LEFT_OUT, GovernanceError
from makna.compiler.plan import plan_request
from makna.compiler.snapshot import Model, ReferenceIssue
from makna.compiler.sqlgen import build_tree
from makna.definitions.loader import load_definitions
from makna.definitions.validate import validate
from makna.model.definitions import Dimension, Filter, Measure
from makna.model.request import ErrorCode, QueryError
from makna.services.examples import check_example_requests

from .compiler_support import DEFINITIONS, lending, request
from .duckdb_support import run_tree
from .test_compiler_refusals import CUSTOMERS, with_derived

BIRTH = f"{CUSTOMERS}.birth_date"


def edited(
    model: Model,
    *,
    dimensions: list[Dimension] | None = None,
    drop_dimensions: tuple[str, ...] = (),
    measures: list[Measure] | None = None,
    metric: str | None = None,
    measure: str | None = None,
    filters: list[Filter] | None = None,
) -> Model:
    """The customer entity with extra dimensions and measures, and a metric `probe` on one."""
    entity = model.entities["customer"]
    kept = [d for d in entity.dimensions if d.name not in drop_dimensions]
    updated = entity.model_copy(
        update={
            "dimensions": [*kept, *(dimensions or [])],
            "measures": [*(entity.measures or []), *(measures or [])],
        }
    )
    metrics = dict(model.metrics)
    if metric is not None:
        update: dict[str, Any] = {"name": metric, "measure": measure}
        if filters is not None:
            update["filters"] = filters
        metrics[metric] = model.metrics["new_customer_count"].model_copy(update=update)
    others = [e for e in model.entities.values() if e.name != "customer"]
    return Model.build(
        [updated, *others], metrics.values(), derived_sensitivity=model.derived_sensitivity
    )


def measure(name: str, agg: str, expr: str | None = None, **more: Any) -> Measure:
    return Measure(
        name=name,
        agg=agg,  # type: ignore[arg-type]
        expr=expr,
        time_dimension="registered_date",
        description="A probe measure.",
        **more,
    )


ASK = {"dimensions": ["customer.registered_date", "customer.city", "customer.gender"]}


# ---- fix 1: only the bare column may be counted ----------------------------------------


def test_a_count_with_a_case_over_a_personal_column_is_stopped_at_compile_time() -> None:
    probe = measure(
        "born_early", "count", "CASE WHEN birth_date < CAST('1975-01-01' AS DATE) THEN 1 END"
    )
    model = edited(lending(), measures=[probe], metric="probe", measure="customer.born_early")
    with pytest.raises(GovernanceError, match="birth_date"):
        compile_request(model, request("probe", **ASK))


def test_a_count_distinct_of_the_bare_personal_column_still_compiles() -> None:
    probe = measure("distinct_births", "count_distinct", "birth_date")
    model = edited(lending(), measures=[probe], metric="probe", measure="customer.distinct_births")
    assert "COUNT(DISTINCT" in compile_request(model, request("probe", **ASK)).sql


# ---- fix 2: the catalog tags columns, not dimensions ------------------------------------


def test_a_column_tagged_personal_by_the_catalog_and_read_only_by_a_measure_is_checked() -> None:
    probe = measure("oldest", "max", "birth_date")
    plain = edited(lending(), drop_dimensions=("birth_year",), measures=[probe],
                   metric="probe", measure="customer.oldest")  # fmt: skip
    # Nothing marks birth_date personal here, so nothing stops the raw maximum ...
    assert "MAX" in compile_request(plain, request("probe", **ASK)).sql
    # ... until the catalog tags the column, although no dimension reads it.
    tagged = with_derived(plain, {BIRTH: "personal"})
    with pytest.raises(GovernanceError, match="birth_date"):
        compile_request(tagged, request("probe", **ASK))


def test_a_catalog_tag_on_the_column_a_dimension_reads_makes_the_dimension_personal() -> None:
    model = with_derived(lending(), {f"{CUSTOMERS}.city": "personal"})
    with pytest.raises(QueryError) as caught:
        plan_request(model, request("new_customer_count", dimensions=["customer.city"]))
    assert caught.value.code == ErrorCode.PERSONAL_DATA_REFUSED


def test_a_metric_filter_on_a_column_only_the_catalog_marks_personal_fails_closed() -> None:
    flt = Filter(dimension="customer.city", op="!=", value="Jakarta")
    model = edited(lending(), metric="probe", measure="customer.new_customers", filters=[flt])
    compile_request(model, request("probe"))  # fine until the catalog tags the column
    tagged = with_derived(model, {f"{CUSTOMERS}.city": "personal"})
    with pytest.raises(GovernanceError, match="city"):
        compile_request(tagged, request("probe"))


# ---- fix 3: a personal official date ----------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [{"grain": "month"}, {"time_start": "2026-01-01"}, {"time_end": "2026-07-01"}],
    ids=["grain", "start", "end"],
)
def test_a_personal_official_date_refuses_a_grain_or_a_time_range(fields: dict[str, str]) -> None:
    model = with_derived(lending(), {f"{CUSTOMERS}.registered_at": "personal"})
    with pytest.raises(QueryError) as caught:
        plan_request(model, request("new_customer_count", **fields))
    assert caught.value.code == ErrorCode.PERSONAL_DATA_REFUSED
    assert caught.value.details["use"] == "time"
    assert caught.value.details["dimension"] == "customer.registered_date"


def test_a_personal_official_date_is_fine_when_the_date_is_not_used() -> None:
    model = with_derived(lending(), {f"{CUSTOMERS}.registered_at": "personal"})
    assert compile_request(model, request("new_customer_count")).sql


def test_a_balance_always_uses_its_date_so_a_personal_date_refuses_it() -> None:
    model = with_derived(
        lending(), {"lending.core.contract_monthly_snapshots.snapshot_date": "personal"}
    )
    with pytest.raises(QueryError) as caught:
        plan_request(model, request("outstanding_principal"))
    assert caught.value.code == ErrorCode.PERSONAL_DATA_REFUSED


# ---- fix 4: V19 reports a governance failure instead of crashing ------------------------


def test_v19_reports_a_dimension_that_copies_a_personal_column(tmp_path: Path) -> None:
    root = tmp_path / "defs"
    shutil.copytree(DEFINITIONS, root)
    customer = root / "lending" / "entities" / "customer.yaml"
    customer.write_text(
        customer.read_text().replace(
            "  - name: gender\n",
            "  - name: dob\n    type: time\n    expr: birth_date\n    sensitivity: internal\n"
            "    description: A copy of the birth date.\n  - name: gender\n",
            1,
        )
    )
    metric = root / "lending" / "metrics" / "new_customer_count.yaml"
    metric.write_text(metric.read_text().replace("customer.customer_segment]", "customer.dob]", 1))
    issues = validate(load_definitions(root), extra_checks=[check_example_requests])
    assert [i.code for i in issues if i.code == "V19"]
    assert {i.code for i in issues} <= {"V19", "V20"}


def test_makna_validate_does_not_crash_on_such_a_dimension(tmp_path: Path) -> None:
    root = tmp_path / "defs"
    shutil.copytree(DEFINITIONS, root)
    customer = root / "lending" / "entities" / "customer.yaml"
    customer.write_text(
        customer.read_text().replace(
            "  - name: gender\n",
            "  - name: dob\n    type: time\n    expr: birth_date\n    sensitivity: internal\n"
            "    description: A copy of the birth date.\n  - name: gender\n",
            1,
        )
    )
    metric = root / "lending" / "metrics" / "new_customer_count.yaml"
    metric.write_text(metric.read_text().replace("customer.customer_segment]", "customer.dob]", 1))
    assert main(["validate", str(root)]) != 0


# ---- fix 5: a minimum below 2 switches the protection off --------------------------------


@pytest.mark.parametrize("bad", [1, 0, -3])
def test_a_minimum_group_size_below_two_is_rejected(bad: int) -> None:
    with pytest.raises(ValueError, match="at least 2"):
        plan_request(lending(), request("new_customer_count"), min_group_size=bad)


def test_two_is_accepted() -> None:
    assert plan_request(lending(), request("new_customer_count"), min_group_size=2)


# ---- fix 6: the warning does not promise what it cannot ---------------------------------


def test_the_warning_claims_only_that_small_groups_are_left_out_of_this_result() -> None:
    response = compile_request(
        lending(), request("new_customer_count", dimensions=["customer.birth_year"])
    )
    [warning] = [w for w in response.warnings if GROUPS_LEFT_OUT in w]
    assert "single person" not in warning
    assert "picked out" not in warning
    assert "left out of this result" in warning


# ---- fix 7: R02 on any entity the plan touches refuses the request -----------------------


def test_a_missing_column_on_the_customer_entity_refuses_a_request_that_reaches_it() -> None:
    row = Dimension(
        name="to_jsonb",
        type="categorical",
        sensitivity="internal",
        description="A column whose name is also a PostgreSQL function of a whole row.",
    )
    model = edited(lending(), dimensions=[row])
    issue = ReferenceIssue("R02", "column to_jsonb is not in lending.core.customers")
    flagged = dataclasses.replace(model, reference_issues={"customer": (issue,)})
    # the metric is measured on the snapshot table and reaches customer through contract
    for metric, fields in (
        ("outstanding_principal", {"dimensions": ["customer.to_jsonb"]}),
        ("new_customer_count", {"dimensions": ["customer.to_jsonb"]}),
        (
            "outstanding_principal",
            {"filters": [{"dimension": "customer.to_jsonb", "op": "=", "value": "x"}]},
        ),
    ):
        with pytest.raises(QueryError) as caught:
            plan_request(flagged, request(metric, **fields))
        assert caught.value.code == ErrorCode.BROKEN_REFERENCE
        assert caught.value.details["issues"][0]["code"] == "R02"


# ---- fix 8: the left-out count of a ratio -----------------------------------------------


def test_the_number_left_out_by_a_ratio_equals_a_reference_count(
    lending_full_db: duckdb.DuckDBPyConnection,
) -> None:
    minimum = 3
    key = "CAST(date_trunc('year', cu.birth_date) AS DATE)"
    base = f"""
    SELECT {key} AS y, cu.province AS p, COUNT(DISTINCT cu.customer_id) AS n
    FROM contract_monthly_snapshots s
    JOIN contracts c ON c.contract_id = s.contract_id
    JOIN customers cu ON cu.customer_id = c.customer_id
    WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM contract_monthly_snapshots) {{extra}}
    GROUP BY 1, 2"""
    num = {
        (y, p): n
        for y, p, n in lending_full_db.execute(
            base.format(extra="AND s.days_past_due > 90")
        ).fetchall()
    }
    den = {(y, p): n for y, p, n in lending_full_db.execute(base.format(extra="")).fetchall()}
    every = set(num) | set(den)
    small = {k for k in every if any(side[k] < minimum for side in (num, den) if k in side)}
    assert small and small != every
    plan = plan_request(
        lending(),
        request("npf_ratio", dimensions=["customer.birth_year", "customer.province"]),
        min_group_size=minimum,
    )
    rows = run_tree(lending_full_db, build_tree(lending(), plan))
    assert {row[-1] for row in rows} == {len(small)}
    assert len(rows) == len(every) - len(small)


# ---- fix 9: wording ---------------------------------------------------------------------


def test_every_sentence_of_the_explanation_starts_with_a_capital_letter() -> None:
    response = compile_request(
        lending(), request("new_customer_count", dimensions=["customer.birth_year"])
    )
    assert all(s[0].isupper() for s in response.explanation), response.explanation


# ---- round 2: only masks the plan built are exempt ---------------------------------------

BUCKET = "FLOOR(CAST({} AS DOUBLE) / 1) * 1"
CATALOG_ONLY = {BIRTH: "personal"}


def test_a_bucket_shaped_max_of_a_catalog_only_personal_column_fails_closed() -> None:
    probe = measure("epoch_max", "max", BUCKET.format("EXTRACT(EPOCH FROM birth_date)"))
    model = edited(lending(), drop_dimensions=("birth_year",), measures=[probe],
                   metric="probe", measure="customer.epoch_max")  # fmt: skip
    assert "MAX" in compile_request(model, request("probe", **ASK)).sql  # untagged: allowed
    with pytest.raises(GovernanceError, match="birth_date"):
        compile_request(with_derived(model, CATALOG_ONLY), request("probe", **ASK))


def test_a_bucket_wrapped_case_in_count_of_a_catalog_only_personal_column_fails_closed() -> None:
    case = "CASE WHEN birth_date < CAST('1975-01-01' AS DATE) THEN 1 END"
    probe = measure("born_early", "count", BUCKET.format(case))
    model = edited(lending(), drop_dimensions=("birth_year",), measures=[probe],
                   metric="probe", measure="customer.born_early")  # fmt: skip
    with pytest.raises(GovernanceError, match="birth_date"):
        compile_request(with_derived(model, CATALOG_ONLY), request("probe", **ASK))


def test_a_mask_shaped_dimension_on_a_catalog_only_personal_column_fails_closed() -> None:
    shaped = Dimension(
        name="epoch_band",
        type="numeric",
        expr=BUCKET.format("EXTRACT(EPOCH FROM birth_date)"),
        sensitivity="internal",
        description="A bucket-shaped copy of the birth date.",
    )
    model = with_derived(edited(lending(), dimensions=[shaped]), CATALOG_ONLY)
    # birth_date is personal, so the dimension is personal too and, without a mask, is refused
    with pytest.raises(QueryError) as caught:
        plan_request(model, request("new_customer_count", dimensions=["customer.epoch_band"]))
    assert caught.value.code == ErrorCode.PERSONAL_DATA_REFUSED


# ---- round 2: column names are matched in lower case --------------------------------------


def test_a_dimension_written_with_capitals_is_refused_when_the_catalog_tags_the_column() -> None:
    shouted = Dimension(
        name="shouted_city",
        type="categorical",
        expr="City",
        sensitivity="internal",
        description="The city, written with a capital.",
    )
    model = with_derived(edited(lending(), dimensions=[shouted]), {f"{CUSTOMERS}.city": "personal"})
    with pytest.raises(QueryError) as caught:
        plan_request(model, request("new_customer_count", dimensions=["customer.shouted_city"]))
    assert caught.value.code == ErrorCode.PERSONAL_DATA_REFUSED


def test_a_derived_key_written_with_capitals_is_read_in_lower_case() -> None:
    model = Model.build(
        lending().entities.values(),
        lending().metrics.values(),
        derived_sensitivity={("Lending.Core.Customers", "City"): "personal"},
    )
    assert model.derived_sensitivity == {(CUSTOMERS, "city"): "personal"}


# ---- round 2: R02 on an entity only a definition filter reaches -------------------------


def test_a_missing_column_on_an_entity_only_a_definition_filter_reaches_refuses_the_request() -> (
    None
):
    flt = Filter(dimension="branch.region_name", op="!=", value="nowhere")
    model = edited(lending(), metric="probe", measure="customer.new_customers", filters=[flt])
    issue = ReferenceIssue("R02", "column region_name is not in lending.core.branches")
    flagged = dataclasses.replace(model, reference_issues={"branch": (issue,)})
    assert compile_request(model, request("probe")).sql  # the filter alone is fine
    with pytest.raises(QueryError) as caught:
        plan_request(flagged, request("probe"))
    assert caught.value.code == ErrorCode.BROKEN_REFERENCE
    assert caught.value.details["issues"][0]["object"] == "entity:branch"


# ---- round 3: result column names, and the mask under each alias -------------------------


def test_two_result_columns_with_the_same_name_fail_with_an_internal_error() -> None:
    # A metric may be called `customer__city`, the name of the column of `customer.city`.
    clash = lending().metrics["new_customer_count"].model_copy(update={"name": "customer__city"})
    model = Model.build(lending().entities.values(), [*lending().metrics.values(), clash])
    with pytest.raises(GovernanceError, match="customer__city"):
        plan_request(model, request("customer__city", dimensions=["customer.city"]))


def test_distinct_result_columns_are_not_affected() -> None:
    plan = plan_request(lending(), request("new_customer_count", dimensions=["customer.city"]))
    assert len({c.name for c in plan.columns}) == len(plan.columns)
