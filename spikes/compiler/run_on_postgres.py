"""Check that the compiler spike's SQL also works on PostgreSQL and DuckDB as
target dialects (decision D-22: PostgreSQL is the MVP target, DuckDB the unit
test engine; Trino is put aside).

The five cases of run_spike.py are compiled for the `postgres` dialect and run
on a real PostgreSQL server (embedded through pgserver) holding the same data in
database `lending`, schema `core`. They are also compiled for the `duckdb`
dialect and run on DuckDB. Each result is compared with the hand-written
reference query run on DuckDB.

Usage: uv run --no-project -p 3.12 --with sqlglot --with duckdb --with pyyaml \
           --with pgserver --with "psycopg[binary]" python spikes/compiler/run_on_postgres.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pgserver
import psycopg

sys.path.insert(0, str(Path(__file__).parent))
import run_spike as rs  # noqa: E402
from mini_compiler import Model, compile_request  # noqa: E402

TABLES = ["branches", "products", "customers", "contracts", "installment_schedules", "payments", "contract_monthly_snapshots"]


def copy_to_postgres(duck, uri: str) -> None:
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute("CREATE DATABASE lending")
    with psycopg.connect(uri, dbname="lending", autocommit=True) as conn:
        conn.execute("CREATE SCHEMA core")
        for t in TABLES:
            cols = duck.execute(f"DESCRIBE lending.core.{t}").fetchall()
            ddl = ", ".join(f"{name} {typ.replace('VARCHAR', 'TEXT')}" for name, typ, *_ in cols)
            conn.execute(f"CREATE TABLE core.{t} ({ddl})")
            rows = duck.execute(f"SELECT * FROM lending.core.{t}").fetchall()
            with conn.cursor() as cur:
                cur.executemany(f"INSERT INTO core.{t} VALUES ({', '.join(['%s'] * len(cols))})", rows)


def main() -> int:
    model = Model.load(rs.EXAMPLES)
    duck = rs.build_db()
    pg = pgserver.get_server(tempfile.mkdtemp())
    failures = 0
    copy_to_postgres(duck, pg.get_uri())
    with psycopg.connect(pg.get_uri(), dbname="lending") as conn:
        print("PostgreSQL", conn.execute("SHOW server_version").fetchone()[0])
        for name, req, ref_sql in rs.CASES:
            want = duck.execute(ref_sql).fetchall()
            pg_sql = compile_request(model, req, dialect="postgres")["sql"]
            got_pg = conn.execute(pg_sql).fetchall()
            got_duck = duck.execute(compile_request(model, req, dialect="duckdb")["sql"]).fetchall()
            ok_pg, ok_duck = rs.same(got_pg, want), rs.same(got_duck, want)
            failures += (not ok_pg) + (not ok_duck)
            print(f"{'PASS' if ok_pg else 'FAIL'} postgres  {'PASS' if ok_duck else 'FAIL'} duckdb  case {name} ({len(got_pg)} rows)")
            if name.startswith("4"):
                print("--- PostgreSQL SQL for case 4\n" + pg_sql)
    print(f"\n{'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
