"""Run the compiler spike: build a tiny lending dataset in DuckDB, compile
requests to Trino SQL, execute the exact Trino SQL text on DuckDB, and compare
each result with an independently hand-written reference query.

Usage: uv run --with sqlglot --with duckdb --with pyyaml python spikes/compiler/run_spike.py
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import sqlglot

sys.path.insert(0, str(Path(__file__).parent))
from mini_compiler import CompileError, Model, compile_request  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "semantic" / "schema" / "examples"


def month_ends(start: date, n: int) -> list[date]:
    out, y, m = [], start.year, start.month
    for _ in range(n):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append(date(ny, nm, 1) - timedelta(days=1))
        y, m = ny, nm
    return out


def build_db() -> duckdb.DuckDBPyConnection:
    """Database `lending`, schema `core`, so Trino three-part names resolve as written."""
    rnd = random.Random(7)
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS lending; CREATE SCHEMA lending.core; USE lending.core")
    con.execute("CREATE TABLE branches(branch_id INT, branch_name VARCHAR, area_name VARCHAR, region_name VARCHAR, province VARCHAR)")
    regions = ["Jawa Barat", "Jawa Timur", "Sumatera", "Kalimantan"]
    for b in range(1, 13):
        con.execute("INSERT INTO branches VALUES (?,?,?,?,?)", [b, f"Branch {b}", f"Area {b % 6}", regions[b % 4], regions[b % 4]])
    con.execute("CREATE TABLE products(product_id INT, product_name VARCHAR, product_category VARCHAR, financing_scheme VARCHAR)")
    cats = [("car_backed", "conventional"), ("motorcycle_backed", "conventional"), ("car_backed", "sharia"), ("unsecured", "conventional")]
    for p, (c, s) in enumerate(cats, 1):
        con.execute("INSERT INTO products VALUES (?,?,?,?)", [p, f"Product {p}", c, s])
    con.execute("CREATE TABLE customers(customer_id INT, full_name VARCHAR, national_id VARCHAR, phone_number VARCHAR, birth_date DATE, customer_segment VARCHAR, occupation_group VARCHAR, province VARCHAR, home_branch_id INT, registered_at TIMESTAMP)")
    for c in range(1, 201):
        con.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?)", [c, f"Person {c}", f"32{c:014d}", f"0812{rnd.randrange(10**7, 10**8)}", date(1960 + c % 40, 1 + c % 12, 1 + c % 28), rnd.choice(["mass", "affluent"]), "employee", rnd.choice(regions), rnd.randrange(1, 13), "2024-01-01 10:00:00"])
    con.execute("CREATE TABLE contracts(contract_id INT, customer_id INT, product_id INT, booking_branch_id INT, servicing_branch_id INT, signed_date DATE, disbursement_date DATE, maturity_date DATE, principal_amount DECIMAL(18,2), tenor_months INT, contract_status VARCHAR, is_restructured BOOLEAN)")
    con.execute("CREATE TABLE installment_schedules(schedule_id INT, contract_id INT, installment_no INT, schedule_version INT, is_current BOOLEAN, due_date DATE, installment_amount DECIMAL(18,2))")
    con.execute("CREATE TABLE payments(payment_id INT, contract_id INT, payment_date DATE, posting_date DATE, principal_paid DECIMAL(18,2), interest_paid DECIMAL(18,2), penalty_paid DECIMAL(18,2), payment_channel VARCHAR, is_reversed BOOLEAN)")
    con.execute("CREATE TABLE contract_monthly_snapshots(snapshot_date DATE, contract_id INT, outstanding_principal DECIMAL(18,2), days_past_due INT, dpd_bucket VARCHAR, collectibility VARCHAR)")
    ends = month_ends(date(2025, 7, 1), 12)  # 2025-07-31 .. 2026-06-30
    sid = pid = 0
    for k in range(1, 401):
        signed = date(2025, 7, 1) + timedelta(days=rnd.randrange(0, 330))
        cancelled = rnd.random() < 0.05
        disb = None if cancelled else signed + timedelta(days=rnd.randrange(0, 6))
        book = rnd.randrange(1, 13)
        serv = book if rnd.random() < 0.7 else rnd.randrange(1, 13)  # 30% transferred
        principal = rnd.randrange(5, 200) * 1_000_000
        restructured = (not cancelled) and rnd.random() < 0.1
        con.execute("INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [k, rnd.randrange(1, 201), rnd.randrange(1, 5), book, serv, signed, disb, signed + timedelta(days=365), principal, 12, "cancelled" if cancelled else "active", restructured])
        if cancelled:
            continue
        inst = round(principal * 1.12 / 12, 2)
        behaviour = rnd.choice(["good"] * 7 + ["late"] * 2 + ["bad"])
        paid_principal = 0.0
        for n in range(1, 13):
            due = disb + timedelta(days=30 * n)
            sid += 1
            superseded = restructured and n > 6
            con.execute("INSERT INTO installment_schedules VALUES (?,?,?,?,?,?,?)", [sid, k, n, 1, not superseded, due, inst])
            if superseded:  # restructured: old rows stay, new rows are current
                sid += 1
                con.execute("INSERT INTO installment_schedules VALUES (?,?,?,?,?,?,?)", [sid, k, n, 2, True, due + timedelta(days=90), round(inst * 0.8, 2)])
            if due > date(2026, 6, 30) or (behaviour == "bad" and n > 2):
                continue
            pay = due + timedelta(days={"good": rnd.randrange(-3, 2), "late": rnd.randrange(5, 40), "bad": 0}[behaviour])
            if pay > date(2026, 6, 30):
                continue
            for rev in ([True, False] if rnd.random() < 0.05 else [False]):  # a reversed attempt, then the real one
                pid += 1
                con.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?)", [pid, k, pay, pay + timedelta(days=rnd.randrange(0, 4)), round(principal / 12, 2), round(inst - principal / 12, 2), 0, rnd.choice(["bank_transfer", "virtual_account", "collector"]), rev])
            paid_principal += principal / 12
        for e in ends:
            if e < disb:
                continue
            # simple, internally consistent position; exact realism is not the point of the spike
            n_due = min(12, max(0, (e - disb).days // 30))
            n_paid = con.execute("SELECT count(*) FROM payments WHERE contract_id=? AND NOT is_reversed AND posting_date<=?", [k, e]).fetchone()[0]
            os_ = round(principal - n_paid * principal / 12, 2)
            dpd = max(0, (n_due - n_paid)) * 30
            bucket = "current" if dpd == 0 else "dpd_1_30" if dpd <= 30 else "dpd_31_60" if dpd <= 60 else "dpd_61_90" if dpd <= 90 else "dpd_91_180" if dpd <= 180 else "dpd_over_180"
            kol = "1" if dpd <= 10 else "2" if dpd <= 90 else "3" if dpd <= 120 else "4" if dpd <= 180 else "5"
            if os_ > 0:
                con.execute("INSERT INTO contract_monthly_snapshots VALUES (?,?,?,?,?,?)", [e, k, os_, dpd, bucket, kol])
    return con


# (name, request, hand-written reference SQL in plain DuckDB, independent of the compiler)
CASES = [
    (
        "1 simple metric, booking-branch join, month grain",
        {"metric": "disbursement_amount", "dimensions": ["branch.region_name"], "grain": "month", "start": "2026-01-01", "end": "2026-07-01"},
        """SELECT CAST(date_trunc('month', c.disbursement_date) AS DATE), b.region_name, sum(c.principal_amount)
           FROM contracts c JOIN branches b ON b.branch_id = c.booking_branch_id
           WHERE c.disbursement_date >= DATE '2026-01-01' AND c.disbursement_date < DATE '2026-07-01'
           GROUP BY 1, 2 ORDER BY 1, 2""",
    ),
    (
        "2 ratio of two balances, two-hop join to servicing branch, quarter grain (last month end per quarter)",
        {"metric": "npf_ratio", "dimensions": ["branch.region_name"], "grain": "quarter", "start": "2025-10-01", "end": "2026-07-01"},
        """WITH pos AS (
             SELECT s.*, b.region_name FROM contract_monthly_snapshots s
             JOIN contracts c USING (contract_id) JOIN branches b ON b.branch_id = c.servicing_branch_id
             WHERE s.snapshot_date IN (DATE '2025-12-31', DATE '2026-03-31', DATE '2026-06-30'))
           SELECT CAST(date_trunc('quarter', snapshot_date) AS DATE), region_name,
                  coalesce(sum(outstanding_principal) FILTER (WHERE days_past_due > 90), 0), sum(outstanding_principal),
                  coalesce(sum(outstanding_principal) FILTER (WHERE days_past_due > 90), 0) / sum(outstanding_principal)
           FROM pos GROUP BY 1, 2 ORDER BY 1, 2""",
    ),
    (
        "3 ratio, no grain, filter on a dimension two hops away (latest snapshot only)",
        {"metric": "npf_ratio", "filters": [{"dimension": "product.product_category", "op": "=", "value": "car_backed"}], "end": "2026-07-01"},
        """SELECT coalesce(sum(s.outstanding_principal) FILTER (WHERE s.days_past_due > 90), 0), sum(s.outstanding_principal),
                  coalesce(sum(s.outstanding_principal) FILTER (WHERE s.days_past_due > 90), 0) / sum(s.outstanding_principal)
           FROM contract_monthly_snapshots s JOIN contracts c USING (contract_id) JOIN products p USING (product_id)
           WHERE s.snapshot_date = DATE '2026-06-30' AND p.product_category = 'car_backed'""",
    ),
    (
        "4 ratio across two fact tables (payments / schedules), multi-hop joins, two dimensions, month grain",
        {"metric": "collection_rate", "dimensions": ["branch.region_name", "product.financing_scheme"], "grain": "month", "start": "2026-01-01", "end": "2026-07-01"},
        """WITH num AS (
             SELECT CAST(date_trunc('month', p.posting_date) AS DATE) m, b.region_name r, pr.financing_scheme f, sum(p.principal_paid + p.interest_paid) v
             FROM payments p JOIN contracts c USING (contract_id) JOIN branches b ON b.branch_id = c.servicing_branch_id JOIN products pr USING (product_id)
             WHERE NOT p.is_reversed AND p.posting_date >= DATE '2026-01-01' AND p.posting_date < DATE '2026-07-01' GROUP BY 1, 2, 3),
           den AS (
             SELECT CAST(date_trunc('month', i.due_date) AS DATE) m, b.region_name r, pr.financing_scheme f, sum(i.installment_amount) v
             FROM installment_schedules i JOIN contracts c USING (contract_id) JOIN branches b ON b.branch_id = c.servicing_branch_id JOIN products pr USING (product_id)
             WHERE i.is_current AND i.due_date >= DATE '2026-01-01' AND i.due_date < DATE '2026-07-01' GROUP BY 1, 2, 3)
           SELECT coalesce(num.m, den.m), coalesce(num.r, den.r), coalesce(num.f, den.f), coalesce(num.v, 0), coalesce(den.v, 0), coalesce(num.v, 0) / nullif(den.v, 0)
           FROM num FULL JOIN den ON num.m = den.m AND num.r = den.r AND num.f = den.f ORDER BY 1, 2, 3""",
    ),
    (
        "5 balance by DPD bucket over a multi-month range (must not sum months)",
        {"metric": "outstanding_principal", "dimensions": ["contract_snapshot.dpd_bucket"], "start": "2026-01-01", "end": "2026-07-01"},
        """SELECT dpd_bucket, sum(outstanding_principal) FROM contract_monthly_snapshots
           WHERE snapshot_date = DATE '2026-06-30' GROUP BY 1 ORDER BY 1""",
    ),
]

NAIVE_COLLECTION_RATE = """
SELECT sum(p.principal_paid + p.interest_paid) / sum(i.installment_amount)
FROM contracts c JOIN payments p USING (contract_id) JOIN installment_schedules i USING (contract_id)
WHERE NOT p.is_reversed AND i.is_current
  AND p.posting_date >= DATE '2026-01-01' AND p.posting_date < DATE '2026-07-01'
  AND i.due_date >= DATE '2026-01-01' AND i.due_date < DATE '2026-07-01'"""


def same(a, b) -> bool:
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        for x, y in zip(ra, rb):
            if isinstance(x, (int, float)) or hasattr(x, "as_tuple"):
                if y is None or abs(float(x) - float(y)) > 1e-9 * max(1.0, abs(float(x))):
                    return False
            elif x != y:
                return False
    return True


def main() -> int:
    model = Model.load(EXAMPLES)
    con = build_db()
    failures = 0
    for name, req, ref_sql in CASES:
        out = compile_request(model, req)  # Trino dialect
        sqlglot.parse_one(out["sql"], dialect="trino")  # must re-parse as Trino
        got = con.execute(out["sql"]).fetchall()  # the exact Trino text, run on DuckDB
        want = con.execute(ref_sql).fetchall()
        ok = same(got, want)
        failures += not ok
        print(f"\n=== Case {name}\nrequest: {req}\n--- Trino SQL\n{out['sql']}\n--- explanation")
        for s in out["explanation"]:
            print("  -", s)
        print(f"--- rows: {len(got)}; first: {got[:2]}\n--- matches reference: {'PASS' if ok else 'FAIL'}")
        if not ok:
            print("want first:", want[:2])

    print("\n=== Why separate aggregation matters")
    right = con.execute(compile_request(model, {"metric": "collection_rate", "start": "2026-01-01", "end": "2026-07-01"})["sql"]).fetchall()[0][-1]
    naive = con.execute(NAIVE_COLLECTION_RATE).fetchone()[0]
    print(f"collection_rate compiled: {right:.4f}; naive row-to-row join of payments and schedules: {float(naive):.4f}")
    failures += abs(right - float(naive)) < 1e-6  # the naive answer must differ, or the data does not exercise the trap

    print("\n=== Governance and error cases")
    checks = [
        ("masked personal dimension is allowed", {"metric": "disbursement_amount", "dimensions": ["customer.phone_number", "customer.birth_year"], "limit": 3}, None),
        ("unmasked personal dimension is refused", {"metric": "disbursement_amount", "dimensions": ["customer.full_name"]}, "PERSONAL_DATA_REFUSED"),
        ("filter on personal data is refused", {"metric": "disbursement_amount", "filters": [{"dimension": "customer.national_id", "op": "=", "value": "3200000000000001"}]}, "PERSONAL_DATA_REFUSED"),
        ("filter on masked personal data is refused", {"metric": "disbursement_amount", "filters": [{"dimension": "customer.phone_number", "op": "=", "value": "081212345678"}]}, "PERSONAL_DATA_REFUSED"),
        ("grain finer than the metric allows", {"metric": "npf_ratio", "grain": "day"}, "INVALID_GRAIN"),
        ("unknown dimension", {"metric": "npf_ratio", "dimensions": ["branch.manager"]}, "UNKNOWN_DIMENSION"),
        ("SQL in a filter value stays a string literal", {"metric": "disbursement_amount", "filters": [{"dimension": "branch.region_name", "op": "=", "value": "x' OR '1'='1"}]}, None),
    ]
    for label, req, want_code in checks:
        try:
            out = compile_request(model, req)
            rows = con.execute(out["sql"]).fetchall()
            got_code = None
        except CompileError as e:
            got_code, rows = e.code, None
        ok = got_code == want_code
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {got_code or 'compiled'}" + (f"; rows: {rows[:2]}" if rows is not None else ""))
    inj = con.execute(compile_request(model, checks[-1][1])["sql"]).fetchall()
    failures += inj != [(None,)]
    print(f"injection attempt returned {inj} (no rows matched, as it must)")

    unpinned = dict(model.metrics["disbursement_amount"])
    unpinned.pop("join_paths")
    model.metrics["disbursement_amount"] = unpinned
    try:
        compile_request(model, {"metric": "disbursement_amount", "dimensions": ["branch.region_name"]})
        print("FAIL  two branch links without a pin compiled")
        failures += 1
    except CompileError as e:
        ok = e.code == "AMBIGUOUS_JOIN_PATH"
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  two branch links without a pin: {e.code}; options: {e.details.get('options')}")

    print(f"\n{'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
