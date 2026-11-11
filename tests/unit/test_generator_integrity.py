"""DAT-1 acceptance 4: keys, relationships and the arithmetic between columns."""

from __future__ import annotations

import duckdb
import pytest

from data.generator import TABLES

from .generator_support import scalar

PRIMARY_KEYS = [(t.name, t.primary_key) for t in TABLES]
FOREIGN_KEYS = [
    (t.name, fk.column, fk.ref_table, fk.ref_column) for t in TABLES for fk in t.foreign_keys
]


@pytest.mark.parametrize(("table", "key"), PRIMARY_KEYS)
def test_primary_keys_are_complete_and_unique(
    db: duckdb.DuckDBPyConnection, table: str, key: tuple[str, ...]
) -> None:
    cols = ", ".join(key)
    rows = scalar(db, f"SELECT count(*) FROM {table}")
    distinct = scalar(db, f"SELECT count(*) FROM (SELECT DISTINCT {cols} FROM {table})")
    nulls = scalar(
        db, f"SELECT count(*) FROM {table} WHERE " + " OR ".join(f"{c} IS NULL" for c in key)
    )
    assert rows == distinct
    assert nulls == 0


@pytest.mark.parametrize(("table", "column", "ref_table", "ref_column"), FOREIGN_KEYS)
def test_every_foreign_key_resolves(
    db: duckdb.DuckDBPyConnection, table: str, column: str, ref_table: str, ref_column: str
) -> None:
    orphans = scalar(
        db,
        f"SELECT count(*) FROM {table} t LEFT JOIN {ref_table} r ON t.{column} = r.{ref_column} "
        f"WHERE t.{column} IS NOT NULL AND r.{ref_column} IS NULL",
    )
    nulls = scalar(db, f"SELECT count(*) FROM {table} WHERE {column} IS NULL")
    assert orphans == 0
    assert nulls == 0


def test_payment_and_schedule_amounts_add_up(db: duckdb.DuckDBPyConnection) -> None:
    assert (
        scalar(
            db,
            "SELECT count(*) FROM payments "
            "WHERE amount_paid <> principal_paid + interest_paid + penalty_paid",
        )
        == 0
    )
    assert (
        scalar(
            db,
            "SELECT count(*) FROM installment_schedules "
            "WHERE installment_amount <> principal_due + interest_due",
        )
        == 0
    )


def test_payments_follow_their_installment_and_contract(db: duckdb.DuckDBPyConnection) -> None:
    assert (
        scalar(
            db,
            "SELECT count(*) FROM payments p JOIN installment_schedules s USING (schedule_id) "
            "WHERE p.contract_id <> s.contract_id OR NOT s.is_current "
            "OR p.payment_date < s.due_date",
        )
        == 0
    )
    lags = db.execute(
        "SELECT min(posting_date - payment_date), max(posting_date - payment_date) FROM payments"
    ).fetchone()
    assert lags == (0, 3)
    assert (
        scalar(
            db,
            "SELECT count(*) FROM payments WHERE penalty_paid > 0 AND payment_date - ("
            "SELECT due_date FROM installment_schedules s "
            "WHERE s.schedule_id = payments.schedule_id) <= 3",
        )
        == 0
    )
    assert scalar(db, "SELECT max(posting_date) FROM payments") <= scalar(
        db, "SELECT DATE '2026-06-30'"
    )


def test_contract_dates_are_ordered(db: duckdb.DuckDBPyConnection) -> None:
    lag = db.execute(
        "SELECT min(disbursement_date - signed_date), max(disbursement_date - signed_date) "
        "FROM contracts"
    ).fetchone()
    assert lag is not None and 0 <= lag[0] and lag[1] <= 5
    assert (
        scalar(
            db,
            "SELECT count(*) FROM contracts WHERE disbursement_date IS NOT NULL "
            "AND (first_due_date <= disbursement_date OR maturity_date < first_due_date)",
        )
        == 0
    )
    assert scalar(db, "SELECT max(disbursement_date) FROM contracts") <= scalar(
        db, "SELECT DATE '2026-06-30'"
    )


def test_contract_status_matches_closing_dates(db: duckdb.DuckDBPyConnection) -> None:
    rows = db.execute(
        "SELECT contract_status, count(*), count(closed_date) FROM contracts GROUP BY 1"
    ).fetchall()
    for status, total, with_closed in rows:
        if status in ("active", "cancelled"):
            assert with_closed == 0, status
        else:
            assert with_closed == total, status
    assert {r[0] for r in rows} == {"active", "closed", "written_off", "cancelled"}


def test_application_decision_fields_follow_the_status(db: duckdb.DuckDBPyConnection) -> None:
    rows = db.execute(
        "SELECT sts_putusan, count(*), count(tgl_putusan), count(appr_amt), count(grade_risiko) "
        "FROM loan_applications GROUP BY 1"
    ).fetchall()
    by_status = {r[0]: r[1:] for r in rows}
    assert set(by_status) <= {"approved", "rejected", "cancelled", "in_process"}
    assert {"approved", "rejected", "cancelled"} <= set(by_status)
    for status, (total, decided, approved, graded) in by_status.items():
        assert decided == (total if status in ("approved", "rejected") else 0), status
        assert graded == decided, status
        assert approved == (total if status == "approved" else 0), status
    assert scalar(db, "SELECT count(*) FROM loan_applications WHERE appr_amt > req_amt") == 0
    assert (
        scalar(
            db,
            "SELECT count(*) FROM loan_applications a "
            "JOIN contracts c ON c.application_id = a.appl_id "
            "WHERE a.sts_putusan <> 'approved'",
        )
        == 0
    )


def test_one_approved_application_leads_to_at_most_one_contract(
    db: duckdb.DuckDBPyConnection,
) -> None:
    assert scalar(db, "SELECT count(*) - count(DISTINCT application_id) FROM contracts") == 0


def test_collection_actions_fill_promise_fields_only_for_promises(
    db: duckdb.DuckDBPyConnection,
) -> None:
    assert (
        scalar(
            db,
            "SELECT count(*) FROM collection_actions WHERE (hasil = 'promise_to_pay') <> "
            "(tgl_janji IS NOT NULL AND nom_janji IS NOT NULL)",
        )
        == 0
    )
    assert scalar(db, "SELECT count(*) FROM collection_actions WHERE tgl_janji <= tgl_aksi") == 0


def test_snapshot_buckets_follow_days_past_due(db: duckdb.DuckDBPyConnection) -> None:
    bad = scalar(
        db,
        """SELECT count(*) FROM contract_monthly_snapshots WHERE
        dpd_bucket <> CASE WHEN days_past_due = 0 THEN 'current'
            WHEN days_past_due <= 30 THEN 'dpd_1_30'
            WHEN days_past_due <= 60 THEN 'dpd_31_60' WHEN days_past_due <= 90 THEN 'dpd_61_90'
            WHEN days_past_due <= 180 THEN 'dpd_91_180' ELSE 'dpd_over_180' END
        OR collectibility <> CASE WHEN days_past_due <= 10 THEN '1'
            WHEN days_past_due <= 90 THEN '2'
            WHEN days_past_due <= 120 THEN '3' WHEN days_past_due <= 180 THEN '4' ELSE '5' END""",
    )
    assert bad == 0
    assert scalar(db, "SELECT min(outstanding_principal) FROM contract_monthly_snapshots") > 0
