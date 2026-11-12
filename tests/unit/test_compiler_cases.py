"""WP-09 criteria 1 and 2: the five spike cases against hand-written reference queries.

The reference SQL below was written from the plain-language definitions and the table layout in
docs/SAMPLE_DOMAIN.md. It does not use the compiler and was not copied from its output. It is
plain DuckDB SQL against the unqualified table names of the fixture (`USE lending.core`).
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest
from sqlglot import exp

from .compiler_support import lending, source_tables, tree_of
from .duckdb_support import run_sql, run_tree, same_rows

CASES: list[tuple[str, dict[str, Any], str]] = [
    (
        "simple metric, booking-branch join, month grain",
        {
            "metric": "disbursement_amount",
            "dimensions": ["branch.region_name"],
            "grain": "month",
            "time_start": "2026-01-01",
            "time_end": "2026-07-01",
        },
        """
        SELECT CAST(date_trunc('month', c.disbursement_date) AS DATE) AS m,
               b.region_name,
               SUM(c.principal_amount)
        FROM contracts c
        LEFT JOIN branches b ON b.branch_id = c.booking_branch_id
        WHERE c.disbursement_date IS NOT NULL
          AND c.disbursement_date >= DATE '2026-01-01'
          AND c.disbursement_date <  DATE '2026-07-01'
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
    ),
    (
        "ratio of two balances, two-hop join to the servicing branch, quarter grain",
        {
            "metric": "npf_ratio",
            "dimensions": ["branch.region_name"],
            "grain": "quarter",
            "time_start": "2025-10-01",
            "time_end": "2026-07-01",
        },
        """
        WITH pos AS (
          SELECT s.snapshot_date, s.outstanding_principal, s.days_past_due, b.region_name
          FROM contract_monthly_snapshots s
          JOIN contracts c ON c.contract_id = s.contract_id
          LEFT JOIN branches b ON b.branch_id = c.servicing_branch_id
          WHERE s.snapshot_date IN (DATE '2025-12-31', DATE '2026-03-31', DATE '2026-06-30')
            AND s.days_past_due <= 210
        ), agg AS (
          SELECT CAST(date_trunc('quarter', snapshot_date) AS DATE) AS q, region_name,
                 COALESCE(SUM(outstanding_principal) FILTER (WHERE days_past_due > 90), 0) AS npf,
                 COALESCE(SUM(outstanding_principal), 0) AS total
          FROM pos GROUP BY 1, 2
        )
        SELECT q, region_name, npf, total, CAST(npf AS DOUBLE) / NULLIF(total, 0)
        FROM agg ORDER BY 1, 2
        """,
    ),
    (
        "ratio with no grain and a filter two joins away (latest snapshot only)",
        {
            "metric": "npf_ratio",
            "filters": [
                {"dimension": "product.product_category", "op": "=", "value": "car_backed"}
            ],
            "time_end": "2026-07-01",
        },
        """
        WITH pos AS (
          SELECT s.outstanding_principal, s.days_past_due
          FROM contract_monthly_snapshots s
          JOIN contracts c ON c.contract_id = s.contract_id
          JOIN products p ON p.product_id = c.product_id
          WHERE s.snapshot_date = DATE '2026-06-30' AND s.days_past_due <= 210
            AND p.product_category = 'car_backed'
        ), agg AS (
          SELECT COALESCE(SUM(outstanding_principal) FILTER (WHERE days_past_due > 90), 0) AS npf,
                 COALESCE(SUM(outstanding_principal), 0) AS total
          FROM pos
        )
        SELECT npf, total, CAST(npf AS DOUBLE) / NULLIF(total, 0) FROM agg
        """,
    ),
    (
        "ratio across two fact tables, multi-hop joins, two dimensions, month grain",
        {
            "metric": "collection_rate",
            "dimensions": ["branch.region_name", "product.financing_scheme"],
            "grain": "month",
            "time_start": "2026-01-01",
            "time_end": "2026-07-01",
        },
        """
        WITH num AS (
          SELECT CAST(date_trunc('month', p.posting_date) AS DATE) AS m,
                 b.region_name AS r, pr.financing_scheme AS f,
                 SUM(p.principal_paid + p.interest_paid) AS v
          FROM payments p
          JOIN contracts c ON c.contract_id = p.contract_id
          LEFT JOIN branches b ON b.branch_id = c.servicing_branch_id
          LEFT JOIN products pr ON pr.product_id = c.product_id
          WHERE NOT p.is_reversed
            AND p.posting_date >= DATE '2026-01-01' AND p.posting_date < DATE '2026-07-01'
          GROUP BY 1, 2, 3
        ), den AS (
          SELECT CAST(date_trunc('month', i.due_date) AS DATE) AS m,
                 b.region_name AS r, pr.financing_scheme AS f,
                 SUM(i.installment_amount) AS v
          FROM installment_schedules i
          JOIN contracts c ON c.contract_id = i.contract_id
          LEFT JOIN branches b ON b.branch_id = c.servicing_branch_id
          LEFT JOIN products pr ON pr.product_id = c.product_id
          WHERE i.is_current
            AND i.due_date >= DATE '2026-01-01' AND i.due_date < DATE '2026-07-01'
          GROUP BY 1, 2, 3
        )
        SELECT COALESCE(num.m, den.m), COALESCE(num.r, den.r), COALESCE(num.f, den.f),
               COALESCE(num.v, 0), COALESCE(den.v, 0),
               CAST(COALESCE(num.v, 0) AS DOUBLE) / NULLIF(COALESCE(den.v, 0), 0)
        FROM num FULL JOIN den
          ON num.m = den.m
         AND num.r IS NOT DISTINCT FROM den.r
         AND num.f IS NOT DISTINCT FROM den.f
        ORDER BY 1, 2, 3
        """,
    ),
    (
        "balance by DPD bucket over a multi-month range (months are not added up)",
        {
            "metric": "outstanding_principal",
            "dimensions": ["contract_snapshot.dpd_bucket"],
            "time_start": "2026-01-01",
            "time_end": "2026-07-01",
        },
        """
        SELECT dpd_bucket, SUM(outstanding_principal)
        FROM contract_monthly_snapshots
        WHERE snapshot_date = DATE '2026-06-30' AND days_past_due <= 210
        GROUP BY 1 ORDER BY 1
        """,
    ),
]


@pytest.mark.parametrize(
    ("request_fields", "reference"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_the_compiled_result_equals_the_hand_written_reference(
    lending_db: duckdb.DuckDBPyConnection, request_fields: dict[str, Any], reference: str
) -> None:
    fields = dict(request_fields)
    tree = tree_of(lending(), fields.pop("metric"), **fields)
    got = run_tree(lending_db, tree)
    want = run_sql(lending_db, reference)
    assert want, "the reference returns no rows, so this case would prove nothing"
    assert same_rows(got, want), f"got {got[:3]} but the reference has {want[:3]}"


# --------------------------------------------------------------------------------------
# Criterion 2: fact tables are never joined

COLLECTION = {
    "dimensions": ["branch.region_name"],
    "time_start": "2026-01-01",
    "time_end": "2026-07-01",
}

NAIVE_COLLECTION_RATE = """
SELECT CAST(SUM(p.principal_paid + p.interest_paid) AS DOUBLE) / SUM(i.installment_amount)
FROM contracts c
JOIN payments p ON p.contract_id = c.contract_id
JOIN installment_schedules i ON i.contract_id = c.contract_id
WHERE NOT p.is_reversed AND i.is_current
  AND p.posting_date >= DATE '2026-01-01' AND p.posting_date < DATE '2026-07-01'
  AND i.due_date >= DATE '2026-01-01' AND i.due_date < DATE '2026-07-01'
"""


def test_collection_rate_never_joins_payments_to_installment_schedules() -> None:
    tree = tree_of(lending(), "collection_rate", **COLLECTION)
    selects = list(tree.find_all(exp.Select))
    readers = [source_tables(s) for s in selects]
    assert any("payments" in r for r in readers)
    assert any("installment_schedules" in r for r in readers)
    for tables in readers:
        assert not {"payments", "installment_schedules"} <= tables
    # And no SELECT joins two tables that both hold facts, whatever their names.
    facts = {
        "payments",
        "installment_schedules",
        "contract_monthly_snapshots",
        "collection_actions",
    }
    for tables in readers:
        assert len(tables & facts) <= 1


def test_collection_rate_differs_from_the_naive_joined_query(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    tree = tree_of(lending(), "collection_rate", time_start="2026-01-01", time_end="2026-07-01")
    rows = run_tree(lending_db, tree)
    assert len(rows) == 1
    correct = float(rows[0][-1])
    naive = float(run_sql(lending_db, NAIVE_COLLECTION_RATE)[0][0])
    assert abs(correct - naive) > 0.01, (correct, naive)


# --------------------------------------------------------------------------------------
# A filter that belongs to the metric definition (here a rule on a joined entity)

RESTRUCTURED_BY_REGION = """
SELECT b.region_name, SUM(s.outstanding_principal)
FROM contract_monthly_snapshots s
JOIN contracts c ON c.contract_id = s.contract_id
LEFT JOIN branches b ON b.branch_id = c.servicing_branch_id
WHERE s.snapshot_date = DATE '2026-06-30' AND s.days_past_due <= 210 AND c.is_restructured
GROUP BY 1 ORDER BY 1
"""


def test_a_metric_level_rule_on_a_joined_entity_filters_the_rows(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    fields: dict[str, Any] = {
        "dimensions": ["branch.region_name"],
        "time_start": "2026-01-01",
        "time_end": "2026-07-01",
    }
    got = run_tree(lending_db, tree_of(lending(), "restructured_outstanding_principal", **fields))
    want = run_sql(lending_db, RESTRUCTURED_BY_REGION)
    assert want and same_rows(got, want)
    everything = run_tree(lending_db, tree_of(lending(), "outstanding_principal", **fields))
    assert not same_rows(got, everything), "the filter removed nothing on this data"
