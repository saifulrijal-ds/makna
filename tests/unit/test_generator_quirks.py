"""DAT-1 acceptance 3: every check of section 4 (Q1 to Q10) on the generated data.

Each test runs on the scale 0.05 data and on the full-scale data (fixture `dataset`).
Q11 (poor catalog metadata) is covered by test_generator_catalog.py and by the column-name
check in test_generator_spec.py.
"""

from __future__ import annotations

import duckdb

from .generator_support import scalar


def test_q1_dates_with_different_meanings_fall_in_different_months(
    db: duckdb.DuckDBPyConnection,
) -> None:
    contracts = scalar(
        db,
        "SELECT avg(CASE WHEN date_trunc('month', signed_date) "
        "<> date_trunc('month', disbursement_date) "
        "THEN 1.0 ELSE 0.0 END) FROM contracts",
    )
    payments = scalar(
        db,
        "SELECT avg(CASE WHEN date_trunc('month', payment_date) "
        "<> date_trunc('month', posting_date) "
        "THEN 1.0 ELSE 0.0 END) FROM payments",
    )
    assert contracts >= 0.03
    assert payments >= 0.03


def test_q2_cancelled_contracts_have_no_money_movement(db: duckdb.DuckDBPyConnection) -> None:
    share = scalar(
        db,
        "SELECT avg(CASE WHEN contract_status = 'cancelled' THEN 1.0 ELSE 0.0 END) FROM contracts",
    )
    assert 0.02 <= share <= 0.06
    assert (
        scalar(
            db,
            "SELECT count(*) FROM contracts WHERE contract_status = 'cancelled' "
            "AND (disbursement_date IS NOT NULL OR principal_amount IS NULL)",
        )
        == 0
    )
    for table in ("installment_schedules", "payments", "contract_monthly_snapshots"):
        rows = scalar(
            db,
            f"SELECT count(*) FROM {table} t JOIN contracts c USING (contract_id) "
            "WHERE c.contract_status = 'cancelled'",
        )
        assert rows == 0, table
    rows = scalar(
        db,
        "SELECT count(*) FROM collection_actions a "
        "JOIN contracts c ON a.kontrak_id = c.contract_id "
        "WHERE c.contract_status = 'cancelled'",
    )
    assert rows == 0
    assert scalar(db, "SELECT count(*) FROM contracts WHERE disbursement_date IS NULL") == scalar(
        db, "SELECT count(*) FROM contracts WHERE contract_status = 'cancelled'"
    )


def test_q3_booking_and_servicing_branches_differ_for_about_a_fifth(
    db: duckdb.DuckDBPyConnection,
) -> None:
    share = scalar(
        db,
        "SELECT avg(CASE WHEN booking_branch_id <> servicing_branch_id THEN 1.0 ELSE 0.0 END) "
        "FROM contracts WHERE disbursement_date IS NOT NULL",
    )
    assert 0.15 <= share <= 0.25
    # Customers also have a home branch and applications a receiving branch.
    differing = scalar(
        db,
        "SELECT count(*) FROM loan_applications a JOIN customers c ON a.cust_id = c.customer_id "
        "WHERE a.cab_id <> c.home_branch_id",
    )
    assert differing > 0


def test_q4_restructured_contracts_keep_their_replaced_rows(db: duckdb.DuckDBPyConnection) -> None:
    share = scalar(
        db,
        "SELECT avg(CASE WHEN is_restructured THEN 1.0 ELSE 0.0 END) FROM contracts "
        "WHERE disbursement_date IS NOT NULL",
    )
    assert 0.06 <= share <= 0.12
    missing = scalar(
        db,
        """SELECT count(*) FROM contracts c WHERE c.is_restructured AND NOT (
            EXISTS (SELECT 1 FROM installment_schedules s
                    WHERE s.contract_id = c.contract_id AND NOT s.is_current)
            AND EXISTS (SELECT 1 FROM installment_schedules s
                        WHERE s.contract_id = c.contract_id AND s.schedule_version = 2))""",
    )
    assert missing == 0
    off = scalar(
        db,
        """SELECT count(*) FROM (
            SELECT c.contract_id, c.principal_amount,
                   sum(CASE WHEN s.is_current THEN s.principal_due ELSE 0 END) AS current_principal
            FROM contracts c JOIN installment_schedules s USING (contract_id) GROUP BY ALL)
           WHERE abs(current_principal - principal_amount) > 1.00""",
    )
    assert off == 0
    # Replaced rows exist only where there was a restructuring, and carry its date.
    stray = scalar(
        db,
        """SELECT count(*) FROM installment_schedules s JOIN contracts c USING (contract_id)
           WHERE (NOT s.is_current OR s.schedule_version = 2) AND
                 (NOT c.is_restructured
                  OR (NOT s.is_current AND s.superseded_date <> c.restructured_date)
                  OR (s.schedule_version = 2 AND s.effective_from <> c.restructured_date))""",
    )
    assert stray == 0


def test_q5_late_and_missing_payments_give_realistic_ratios(db: duckdb.DuckDBPyConnection) -> None:
    npf = scalar(
        db,
        "SELECT sum(CASE WHEN days_past_due > 90 THEN outstanding_principal ELSE 0 END) "
        "/ sum(outstanding_principal) FROM contract_monthly_snapshots "
        "WHERE snapshot_date = DATE '2026-06-30'",
    )
    assert 0.01 <= npf <= 0.08
    collected = scalar(
        db,
        "SELECT sum(principal_paid + interest_paid) FROM payments WHERE NOT is_reversed "
        "AND posting_date BETWEEN DATE '2026-01-01' AND DATE '2026-06-30'",
    )
    due = scalar(
        db,
        "SELECT sum(installment_amount) FROM installment_schedules WHERE is_current "
        "AND due_date BETWEEN DATE '2026-01-01' AND DATE '2026-06-30'",
    )
    assert 0.85 <= collected / due <= 1.0
    late_penalties = scalar(
        db,
        "SELECT count(*) FROM payments p JOIN installment_schedules s USING (schedule_id) "
        "WHERE p.payment_date - s.due_date > 3 AND p.penalty_paid = 0",
    )
    assert late_penalties == 0


def test_q6_about_three_percent_of_payments_are_reversed(db: duckdb.DuckDBPyConnection) -> None:
    share = scalar(db, "SELECT avg(CASE WHEN is_reversed THEN 1.0 ELSE 0.0 END) FROM payments")
    assert 0.02 <= share <= 0.04
    # Most reversed payments are followed by a successful payment on the same installment.
    followed = scalar(
        db,
        """SELECT avg(CASE WHEN EXISTS (SELECT 1 FROM payments q WHERE q.schedule_id = p.schedule_id
                                         AND NOT q.is_reversed) THEN 1.0 ELSE 0.0 END)
           FROM payments p WHERE p.is_reversed""",
    )
    assert followed > 0.5


def test_q7_balances_equal_principal_minus_posted_principal(db: duckdb.DuckDBPyConnection) -> None:
    wrong = scalar(
        db,
        """SELECT count(*) FROM (
            SELECT s.outstanding_principal AS actual,
                   c.principal_amount - coalesce(sum(p.principal_paid), 0) AS expected
            FROM contract_monthly_snapshots s
            JOIN contracts c ON c.contract_id = s.contract_id
            LEFT JOIN payments p ON p.contract_id = s.contract_id AND NOT p.is_reversed
                                 AND p.posting_date <= s.snapshot_date
            GROUP BY s.snapshot_date, s.contract_id, s.outstanding_principal, c.principal_amount)
           WHERE abs(actual - expected) > 0.01""",
    )
    assert wrong == 0


def test_q7_days_past_due_follow_the_unpaid_installments(db: duckdb.DuckDBPyConnection) -> None:
    """Recompute days_past_due from schedules and payments, as the derivation in section 4 says."""
    wrong = scalar(
        db,
        """SELECT count(*) FROM (
            SELECT s.days_past_due AS actual,
                   coalesce(date_diff('day', min(i.due_date), s.snapshot_date), 0) AS expected
            FROM contract_monthly_snapshots s
            LEFT JOIN installment_schedules i
              ON i.contract_id = s.contract_id AND i.effective_from <= s.snapshot_date
             AND (i.superseded_date IS NULL OR i.superseded_date > s.snapshot_date)
             AND i.due_date < s.snapshot_date
             AND coalesce((SELECT sum(p.principal_paid + p.interest_paid) FROM payments p
                           WHERE p.schedule_id = i.schedule_id AND NOT p.is_reversed
                             AND p.posting_date <= s.snapshot_date), 0) < i.installment_amount
            GROUP BY s.snapshot_date, s.contract_id, s.days_past_due)
           WHERE actual <> expected""",
    )
    assert wrong == 0


def test_q7_a_balance_summed_over_a_quarter_is_not_the_portfolio(
    db: duckdb.DuckDBPyConnection,
) -> None:
    months = db.execute(
        "SELECT snapshot_date, sum(outstanding_principal) FROM contract_monthly_snapshots "
        "WHERE snapshot_date IN (DATE '2026-04-30', DATE '2026-05-31', DATE '2026-06-30') "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert len(months) == 3
    assert sum(m[1] for m in months) > 2 * months[-1][1]


def test_q8_contracts_leave_the_book_when_closed_or_written_off(
    db: duckdb.DuckDBPyConnection,
) -> None:
    after = scalar(
        db,
        "SELECT count(*) FROM contract_monthly_snapshots s JOIN contracts c USING (contract_id) "
        "WHERE s.snapshot_date > c.closed_date",
    )
    assert after == 0
    # A contract past 210 days due at a month end is written off at that month end.
    over = db.execute(
        "SELECT s.snapshot_date, c.contract_status, c.closed_date "
        "FROM contract_monthly_snapshots s "
        "JOIN contracts c USING (contract_id) WHERE s.days_past_due > 210"
    ).fetchall()
    assert over
    assert all(status == "written_off" and closed == snap for snap, status, closed in over)
    written_off_wrong = scalar(
        db,
        """SELECT count(*) FROM contracts c WHERE contract_status = 'written_off' AND NOT (
            closed_date = (SELECT max(snapshot_date) FROM contract_monthly_snapshots s
                           WHERE s.contract_id = c.contract_id)
            AND (SELECT max(days_past_due) FROM contract_monthly_snapshots s
                 WHERE s.contract_id = c.contract_id) > 210)""",
    )
    assert written_off_wrong == 0
    # Fully repaid contracts are closed and nothing is owed on their closing date.
    closed_wrong = scalar(
        db,
        """SELECT count(*) FROM contracts c WHERE contract_status = 'closed' AND
            c.principal_amount - (SELECT coalesce(sum(principal_paid), 0) FROM payments p
                                  WHERE p.contract_id = c.contract_id AND NOT p.is_reversed
                                    AND p.posting_date <= c.closed_date) > 0.01""",
    )
    assert closed_wrong == 0
    # Active contracts still owe principal at the as-of date.
    active_wrong = scalar(
        db,
        """SELECT count(*) FROM contracts c WHERE contract_status = 'active' AND NOT EXISTS (
            SELECT 1 FROM contract_monthly_snapshots s
            WHERE s.contract_id = c.contract_id AND s.snapshot_date = DATE '2026-06-30')""",
    )
    assert active_wrong == 0


def test_q9_personal_data_is_visibly_fake(db: duckdb.DuckDBPyConnection) -> None:
    rows = scalar(db, "SELECT count(*) FROM customers")
    assert (
        scalar(
            db, "SELECT count(*) FROM customers WHERE regexp_matches(national_id, '^99[0-9]{14}$')"
        )
        == rows
    )
    assert scalar(db, "SELECT count(*) FROM customers WHERE phone_number LIKE '0899000%'") == rows
    assert scalar(db, "SELECT count(*) FROM customers WHERE email LIKE '%@example.com'") == rows
    assert scalar(db, "SELECT count(DISTINCT national_id) FROM customers") == rows
    assert (
        scalar(db, "SELECT count(*) FROM customers WHERE full_name IS NULL OR birth_date IS NULL")
        == 0
    )


def test_q10_region_is_not_province(db: duckdb.DuckDBPyConnection) -> None:
    spanning = db.execute(
        "SELECT region_name FROM branches GROUP BY 1 HAVING count(DISTINCT province) > 1"
    ).fetchall()
    assert spanning
    regions = {r[0] for r in db.execute("SELECT DISTINCT region_name FROM branches").fetchall()}
    provinces = {r[0] for r in db.execute("SELECT DISTINCT province FROM branches").fetchall()}
    assert regions == {
        "Jawa Barat",
        "Jawa Tengah",
        "Jawa Timur",
        "Sumatera",
        "Kalimantan Sulawesi",
    }
    assert "Jawa Barat" in regions & provinces
    assert scalar(db, "SELECT count(DISTINCT area_name) FROM branches") == 12
    assert scalar(db, "SELECT count(*) FROM branches WHERE NOT is_active") == 2


def test_closed_branches_still_have_old_contracts(db: duckdb.DuckDBPyConnection) -> None:
    old = scalar(
        db,
        "SELECT count(*) FROM contracts c JOIN branches b ON b.branch_id = c.booking_branch_id "
        "WHERE NOT b.is_active",
    )
    assert old > 0
    serviced = scalar(
        db,
        "SELECT count(*) FROM contracts c JOIN branches b ON b.branch_id = c.servicing_branch_id "
        "WHERE NOT b.is_active AND c.disbursement_date IS NOT NULL",
    )
    assert serviced == 0
