"""WP-04B criterion 1 (D-31): a contract written off at a month end is not in that month end's
outstanding principal, NPF, PAR 30, or active count.

Quirk Q8 of `docs/SAMPLE_DOMAIN.md`: a contract more than 210 days past due at a month end is
written off *at* that month end and has no later snapshot rows, so its write-off row is still a
snapshot row. The reference queries below are written by hand from the contract's own status
(`contract_status = 'written_off'` and `closed_date` on or before the snapshot date), not from the
rule `in_book` or from the compiler, so they check the definitions and not themselves.
"""

from __future__ import annotations

import datetime
from typing import Any

import duckdb
import pytest

from .compiler_support import lending, tree_of
from .duckdb_support import run_sql, run_tree, same_rows

# The snapshot rows that are in the book: the contract is not written off on or before the date.
IN_BOOK = """
NOT EXISTS (
  SELECT 1 FROM contracts c
  WHERE c.contract_id = s.contract_id
    AND c.contract_status = 'written_off'
    AND c.closed_date <= s.snapshot_date
)
"""

# (metric, aggregate over the rows of one month end)
REFERENCES = {
    "outstanding_principal": "SUM(s.outstanding_principal)",
    "npf_outstanding_principal": "SUM(s.outstanding_principal) FILTER (WHERE s.days_past_due > 90)",
    "par30_outstanding_principal": (
        "SUM(s.outstanding_principal) FILTER (WHERE s.days_past_due > 30)"
    ),
    "active_contract_count": "COUNT(*)",
}


def write_off_month_ends(con: duckdb.DuckDBPyConnection) -> list[datetime.date]:
    """Month ends at which some contract was written off, found from the contracts."""
    rows = run_sql(
        con,
        """
        SELECT DISTINCT s.snapshot_date
        FROM contract_monthly_snapshots s
        JOIN contracts c ON c.contract_id = s.contract_id
        WHERE c.contract_status = 'written_off' AND c.closed_date = s.snapshot_date
        ORDER BY 1
        """,
    )
    return [row[0] for row in rows]


def scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    return run_sql(con, sql)[0][0]


def month_range(month_end: datetime.date) -> dict[str, str]:
    start = month_end.replace(day=1)
    following = (month_end + datetime.timedelta(days=1)).isoformat()
    return {"time_start": start.isoformat(), "time_end": following}


def reference(
    con: duckdb.DuckDBPyConnection, metric: str, month_end: datetime.date, *, in_book: bool
) -> Any:
    condition = IN_BOOK if in_book else "TRUE"
    sql = f"""
        SELECT {REFERENCES[metric]}
        FROM contract_monthly_snapshots s
        WHERE s.snapshot_date = DATE '{month_end.isoformat()}' AND {condition}
    """
    return run_sql(con, sql)


def test_the_fixture_has_contracts_written_off_at_a_month_end(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    month_ends = write_off_month_ends(lending_db)
    assert month_ends, "the fixture has no write-off, so the checks below would prove nothing"
    # The write-off row is a snapshot row, more than 210 days past due (Q8): the very row that
    # the portfolio metrics must leave out and that the rule write_off_position selects.
    at_write_off = scalar(
        lending_db,
        """
        SELECT COUNT(*) FROM contract_monthly_snapshots s
        JOIN contracts c ON c.contract_id = s.contract_id
        WHERE c.contract_status = 'written_off' AND c.closed_date = s.snapshot_date
          AND s.days_past_due > 210
        """,
    )
    written_off = scalar(
        lending_db, "SELECT COUNT(*) FROM contracts WHERE contract_status = 'written_off'"
    )
    assert at_write_off == written_off > 0


@pytest.mark.parametrize("metric", sorted(REFERENCES))
def test_a_contract_written_off_at_a_month_end_is_not_in_that_month_ends_figure(
    lending_db: duckdb.DuckDBPyConnection, metric: str
) -> None:
    month_ends = write_off_month_ends(lending_db)
    assert month_ends
    for month_end in month_ends:
        got = run_tree(lending_db, tree_of(lending(), metric, **month_range(month_end)))
        without = reference(lending_db, metric, month_end, in_book=True)
        including = reference(lending_db, metric, month_end, in_book=False)
        assert same_rows(got, without), (metric, month_end, got, without)
        assert not same_rows(got, including), (metric, month_end, "the write-off row is counted")


def test_the_npf_ratio_leaves_the_write_off_row_out_of_both_parts(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    for month_end in write_off_month_ends(lending_db):
        got = run_tree(lending_db, tree_of(lending(), "npf_ratio", **month_range(month_end)))
        npf = reference(lending_db, "npf_outstanding_principal", month_end, in_book=True)[0][0]
        total = reference(lending_db, "outstanding_principal", month_end, in_book=True)[0][0]
        npf_all = reference(lending_db, "npf_outstanding_principal", month_end, in_book=False)[0][0]
        total_all = reference(lending_db, "outstanding_principal", month_end, in_book=False)[0][0]
        assert len(got) == 1
        assert float(got[0][-1]) == pytest.approx(float(npf) / float(total))
        assert float(got[0][-1]) != pytest.approx(float(npf_all) / float(total_all))


def test_the_write_off_row_still_exists_for_the_draft_measure_to_see(
    lending_db: duckdb.DuckDBPyConnection,
) -> None:
    """The fix removes the row from the portfolio metrics, not from the data: the rule that
    selects exactly that row is the one the draft `write_off_amount` is built on."""
    month_end = write_off_month_ends(lending_db)[0]
    selected = scalar(
        lending_db,
        f"""
        SELECT COUNT(*) FROM contract_monthly_snapshots s
        WHERE s.snapshot_date = DATE '{month_end.isoformat()}' AND s.days_past_due > 210
        """,
    )
    written_off = scalar(
        lending_db,
        f"""
        SELECT COUNT(*) FROM contracts
        WHERE contract_status = 'written_off' AND closed_date = DATE '{month_end.isoformat()}'
        """,
    )
    assert selected == written_off > 0
