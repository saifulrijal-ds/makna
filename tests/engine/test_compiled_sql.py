"""WP-11 acceptance 3 and 4: the SQL the product compiler prints for `postgres` runs on the
sample database.

Criterion 3: the five cases of `spikes/compiler/run_on_postgres.py` give the same rows as their
hand-written reference queries. The reference queries are copied from `spikes/compiler/
run_spike.py` (`CASES`), where they were written without the compiler, and only changed to name
tables as `core.<table>` (see `docs/notes/reference-sql-order-must-not-depend-on-the-locale.md`).
They are not derived from compiler output. Row order is not compared.

Criterion 4: every approved sample metric compiles with no dimensions at its default grain,
runs, returns at least one row, and returns the columns of ARCHITECTURE section 7.2.

Requests use the compiler's defaults: no governance argument is passed.
"""

# ruff: noqa: E501  (the reference queries are copied from the spike as written there)

from __future__ import annotations

from functools import cache
from typing import Any

import psycopg
import pytest

from makna.compiler.compile import compile_request
from makna.compiler.snapshot import Model
from makna.definitions.loader import load_definitions
from makna.model.definitions import Metric
from makna.model.request import QueryRequest
from makna.services.examples import model_from_definitions
from tests.catalog_support import DEFINITIONS

from .support import run_compiled, run_reference, same_rows


@cache
def lending() -> Model:
    return model_from_definitions(load_definitions(DEFINITIONS))


def compile_sql(request: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    response = compile_request(lending(), QueryRequest.model_validate(request), "postgres")
    assert response.dialect == "postgres"
    return response.sql, tuple(column.name for column in response.columns)


# (name, request, hand-written reference SQL, expected result columns)
CASES: list[tuple[str, dict[str, Any], str, list[str]]] = [
    (
        "1 simple metric, booking-branch join, month grain",
        {
            "metric": "disbursement_amount",
            "dimensions": ["branch.region_name"],
            "grain": "month",
            "time_start": "2026-01-01",
            "time_end": "2026-07-01",
        },
        """SELECT CAST(date_trunc('month', c.disbursement_date) AS DATE), b.region_name, sum(c.principal_amount)
           FROM core.contracts c JOIN core.branches b ON b.branch_id = c.booking_branch_id
           WHERE c.disbursement_date >= DATE '2026-01-01' AND c.disbursement_date < DATE '2026-07-01'
           GROUP BY 1, 2 ORDER BY 1, 2""",
        ["metric_time", "branch__region_name", "disbursement_amount"],
    ),
    (
        "2 ratio of two balances, two-hop join to servicing branch, quarter grain",
        {
            "metric": "npf_ratio",
            "dimensions": ["branch.region_name"],
            "grain": "quarter",
            "time_start": "2025-10-01",
            "time_end": "2026-07-01",
        },
        """WITH pos AS (
             SELECT s.*, b.region_name FROM core.contract_monthly_snapshots s
             JOIN core.contracts c USING (contract_id) JOIN core.branches b ON b.branch_id = c.servicing_branch_id
             WHERE s.snapshot_date IN (DATE '2025-12-31', DATE '2026-03-31', DATE '2026-06-30'))
           SELECT CAST(date_trunc('quarter', snapshot_date) AS DATE), region_name,
                  coalesce(sum(outstanding_principal) FILTER (WHERE days_past_due > 90), 0), sum(outstanding_principal),
                  coalesce(sum(outstanding_principal) FILTER (WHERE days_past_due > 90), 0) / sum(outstanding_principal)
           FROM pos GROUP BY 1, 2 ORDER BY 1, 2""",
        [
            "metric_time",
            "branch__region_name",
            "npf_outstanding_principal",
            "outstanding_principal",
            "npf_ratio",
        ],
    ),
    (
        "3 ratio, no grain, filter on a dimension two hops away (latest snapshot only)",
        {
            "metric": "npf_ratio",
            "filters": [{"dimension": "product.product_category", "op": "=", "value": "car_backed"}],
            "time_end": "2026-07-01",
        },
        """SELECT coalesce(sum(s.outstanding_principal) FILTER (WHERE s.days_past_due > 90), 0), sum(s.outstanding_principal),
                  coalesce(sum(s.outstanding_principal) FILTER (WHERE s.days_past_due > 90), 0) / sum(s.outstanding_principal)
           FROM core.contract_monthly_snapshots s JOIN core.contracts c USING (contract_id) JOIN core.products p USING (product_id)
           WHERE s.snapshot_date = DATE '2026-06-30' AND p.product_category = 'car_backed'""",
        ["npf_outstanding_principal", "outstanding_principal", "npf_ratio"],
    ),
    (
        "4 ratio across two fact tables, multi-hop joins, two dimensions, month grain",
        {
            "metric": "collection_rate",
            "dimensions": ["branch.region_name", "product.financing_scheme"],
            "grain": "month",
            "time_start": "2026-01-01",
            "time_end": "2026-07-01",
        },
        """WITH num AS (
             SELECT CAST(date_trunc('month', p.posting_date) AS DATE) m, b.region_name r, pr.financing_scheme f, sum(p.principal_paid + p.interest_paid) v
             FROM core.payments p JOIN core.contracts c USING (contract_id) JOIN core.branches b ON b.branch_id = c.servicing_branch_id JOIN core.products pr USING (product_id)
             WHERE NOT p.is_reversed AND p.posting_date >= DATE '2026-01-01' AND p.posting_date < DATE '2026-07-01' GROUP BY 1, 2, 3),
           den AS (
             SELECT CAST(date_trunc('month', i.due_date) AS DATE) m, b.region_name r, pr.financing_scheme f, sum(i.installment_amount) v
             FROM core.installment_schedules i JOIN core.contracts c USING (contract_id) JOIN core.branches b ON b.branch_id = c.servicing_branch_id JOIN core.products pr USING (product_id)
             WHERE i.is_current AND i.due_date >= DATE '2026-01-01' AND i.due_date < DATE '2026-07-01' GROUP BY 1, 2, 3)
           SELECT coalesce(num.m, den.m), coalesce(num.r, den.r), coalesce(num.f, den.f), coalesce(num.v, 0), coalesce(den.v, 0), coalesce(num.v, 0) / nullif(den.v, 0)
           FROM num FULL JOIN den ON num.m = den.m AND num.r = den.r AND num.f = den.f ORDER BY 1, 2, 3""",
        [
            "metric_time",
            "branch__region_name",
            "product__financing_scheme",
            "collected_amount",
            "due_amount",
            "collection_rate",
        ],
    ),
    (
        "5 balance by DPD bucket over a multi-month range (must not sum months)",
        {
            "metric": "outstanding_principal",
            "dimensions": ["contract_snapshot.dpd_bucket"],
            "time_start": "2026-01-01",
            "time_end": "2026-07-01",
        },
        """SELECT dpd_bucket, sum(outstanding_principal) FROM core.contract_monthly_snapshots
           WHERE snapshot_date = DATE '2026-06-30' GROUP BY 1 ORDER BY 1""",
        ["contract_snapshot__dpd_bucket", "outstanding_principal"],
    ),
]  # fmt: skip


@pytest.mark.parametrize(
    ("name", "request_", "reference", "columns"), CASES, ids=[f"case{c[0][0]}" for c in CASES]
)
def test_a_spike_case_compiled_by_the_product_equals_its_reference_query(
    engine_connection: psycopg.Connection[Any],
    name: str,
    request_: dict[str, Any],
    reference: str,
    columns: list[str],
) -> None:
    sql, planned = compile_sql(request_)
    names, got = run_compiled(engine_connection, sql)
    _, want = run_reference(engine_connection, reference)
    assert want, "the reference query returned nothing; the case proves nothing"
    assert names == columns == list(planned)
    assert same_rows(got, want) is None, name


def approved_metrics() -> list[Metric]:
    return sorted(
        (m for m in lending().metrics.values() if m.status == "approved"), key=lambda m: m.name
    )


def expected_columns(metric: Metric) -> list[str]:
    """Section 7.2, written out from the metric's own type: `metric_time` when a grain is
    requested, then the inputs of a ratio or derived metric, then the metric."""
    columns = ["metric_time"] if metric.default_grain else []
    if metric.type == "ratio":
        assert metric.numerator and metric.denominator
        columns += [metric.numerator, metric.denominator]
    elif metric.type == "derived":
        columns += list(metric.inputs or [])
    return [*columns, metric.name]


def test_the_sample_has_approved_metrics_of_every_kind_the_test_covers() -> None:
    kinds = {m.type for m in approved_metrics()}
    assert len(approved_metrics()) >= 20
    assert {"simple", "ratio"} <= kinds


@pytest.mark.parametrize("metric", approved_metrics(), ids=lambda m: m.name)
def test_every_approved_metric_runs_at_its_default_grain_with_no_dimensions(
    engine_connection: psycopg.Connection[Any], metric: Metric
) -> None:
    request_: dict[str, Any] = {"metric": metric.name}
    if metric.default_grain:
        request_["grain"] = metric.default_grain
    sql, planned = compile_sql(request_)
    names, rows = run_compiled(engine_connection, sql)
    assert rows, f"{metric.name} returned no row"
    assert names == expected_columns(metric) == list(planned)
