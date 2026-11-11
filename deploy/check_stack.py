"""Checks on a running development stack (CI `stack` job; run it locally after `up -d --wait`).

    uv run python -m deploy.check_stack

Proves WP-05 acceptance 2 (row counts), 4 (a second `init` changes nothing) and 6 (roles and
the published port), and that `init` synced the sample catalog with no open reference issue
(WP-06). Passwords default to the development values of docker-compose.yml.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import psycopg
import pyarrow.parquet as pq

from data.generator.spec import TABLES

COMPOSE = ["docker", "compose", "-f", "deploy/docker-compose.yml"]
HOST = "127.0.0.1"
PORT = os.environ.get("MAKNA_POSTGRES_PORT", "5432")
PASSWORDS = {
    "makna": os.environ.get("MAKNA_DB_PASSWORD", "makna_dev_makna"),
    "lending_loader": os.environ.get("MAKNA_LENDING_LOADER_PASSWORD", "makna_dev_loader"),
    "lending_reader": os.environ.get("MAKNA_LENDING_READER_PASSWORD", "makna_dev_reader"),
}


def connect(role: str, database: str) -> psycopg.Connection:
    return psycopg.connect(
        host=HOST,
        port=PORT,
        user=role,
        password=PASSWORDS[role],
        dbname=database,
        connect_timeout=10,
        autocommit=True,
    )


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*COMPOSE, *args], capture_output=True, text=True, check=check)


def fetch_generated_files(target: Path) -> None:
    compose("cp", "init:/app/data/generated/.", str(target))


def check_row_counts() -> None:
    """Acceptance 2: count(*) with the read-only role equals the generated file's rows."""
    with tempfile.TemporaryDirectory() as directory:
        fetch_generated_files(Path(directory))
        expected = {
            t.name: pq.ParquetFile(Path(directory) / f"{t.name}.parquet").metadata.num_rows
            for t in TABLES
        }
    assert len(expected) == 9 and all(expected.values()), expected
    with connect("lending_reader", "lending") as connection:
        for name, rows in expected.items():
            found = connection.execute(f"SELECT count(*) FROM lending.core.{name}").fetchone()
            assert found is not None and found[0] == rows, f"{name}: {found} != {rows}"


def check_catalog_synced() -> None:
    """WP-06: `init` synced the sample export, and the sample definitions have no open issue."""
    with connect("makna", "makna") as connection:
        datasets = connection.execute(
            "SELECT count(*) FROM catalog_dataset WHERE removed_at IS NULL"
        ).fetchone()
        columns = connection.execute("SELECT count(*) FROM catalog_column").fetchone()
        issues = connection.execute(
            "SELECT count(*) FROM reference_issue WHERE closed_at IS NULL"
        ).fetchone()
    assert datasets is not None and datasets[0] == len(TABLES), datasets
    assert columns is not None and columns[0] == sum(len(t.columns) for t in TABLES), columns
    assert issues is not None and issues[0] == 0, f"{issues} open reference issues"


def snapshot() -> dict[str, object]:
    """Everything a second `init` run could change: rows, load markers, tuple counters."""
    state: dict[str, object] = {}
    for role, database in (("lending_loader", "lending"), ("makna", "makna")):
        with connect(role, database) as connection:
            connection.execute("SELECT pg_stat_clear_snapshot()")
            rows = connection.execute(
                "SELECT s.schemaname || '.' || s.relname, s.n_tup_ins, s.n_tup_upd,"
                " s.n_tup_del, obj_description(s.relid, 'pg_class')"
                " FROM pg_stat_user_tables s ORDER BY 1"
            ).fetchall()
            state[database] = rows
            if database == "makna":
                found = connection.execute("SELECT version_num FROM alembic_version").fetchall()
                state["alembic_version"] = found
    return state


def check_second_init_changes_nothing() -> None:
    """Acceptance 4: a second `init` exits 0 and changes no row, marker, or revision."""
    import time

    time.sleep(2)  # let PostgreSQL flush the statistics of the first run
    before = snapshot()
    result = compose("run", "--rm", "init", check=False)
    assert result.returncode == 0, (
        f"init exited {result.returncode}:\n{result.stdout}{result.stderr}"
    )
    output = result.stdout + result.stderr
    for step in ("generate-sample-data", "load-lending", "migrate", "sync-catalog"):
        assert f"init: {step}: unchanged" in output, (
            f"step {step} did not report unchanged:\n{output}"
        )
    time.sleep(2)
    after = snapshot()
    assert before == after, f"state changed:\nbefore={before}\nafter={after}"


INSUFFICIENT_PRIVILEGE = "42501"


def expect_connect_denied(role: str, database: str) -> None:
    """The server answered and refused the CONNECT privilege.

    libpq reports no SQLSTATE for a failed login, so the server's own FATAL message is the
    evidence (it carries SQLSTATE 42501 on the wire). An unreachable server has another
    message and fails this check.
    """
    try:
        connect(role, database).close()
    except psycopg.OperationalError as error:
        text = str(error)
        assert f'FATAL:  permission denied for database "{database}"' in text, (
            f"{role} -> {database}: not a CONNECT denial: {text}"
        )
        assert "User does not have CONNECT privilege" in text, text
        return
    raise AssertionError(f"{role} could connect to {database}")


def expect_privilege_denied(role: str, database: str, statement: str) -> None:
    """Run `statement` with read-only mode off, so only privileges can stop it (42501)."""
    with connect(role, database) as connection:  # a failure to connect propagates: not "denied"
        connection.execute("SET default_transaction_read_only = off")
        try:
            connection.execute(statement)
        except psycopg.Error as error:
            assert error.sqlstate == INSUFFICIENT_PRIVILEGE, (
                f"{role}: {statement!r}: expected SQLSTATE {INSUFFICIENT_PRIVILEGE}, "
                f"got {error.sqlstate}: {error}"
            )
            return
    raise AssertionError(f"{role} was allowed: {statement}")


def check_roles() -> None:
    """Acceptance 6: makna cannot reach lending; the reader can only read it."""
    expect_connect_denied("makna", "lending")
    with connect("makna", "makna") as connection:  # and the role does work on its own database
        assert connection.execute("SELECT count(*) FROM catalog_source").fetchone() == (0,)
    expect_connect_denied("lending_reader", "makna")
    with connect("lending_reader", "lending") as connection:
        assert connection.execute("SELECT count(*) FROM lending.core.branches").fetchone()
    for statement in (
        "INSERT INTO lending.core.branches SELECT * FROM lending.core.branches LIMIT 1",
        "UPDATE lending.core.branches SET branch_name = 'x'",
        "DELETE FROM lending.core.branches",
        "TRUNCATE lending.core.branches",
        "DROP TABLE lending.core.branches",
        "ALTER TABLE lending.core.branches ADD COLUMN scratch integer",
        "CREATE TABLE lending.core.scratch (a integer)",
        "CREATE TABLE public.scratch (a integer)",
    ):
        expect_privilege_denied("lending_reader", "lending", statement)


def check_published_ports() -> None:
    """Acceptance 6: PostgreSQL (and the app) are published on 127.0.0.1 only."""
    containers = compose("ps", "-q", "postgres", "app").stdout.split()
    assert len(containers) == 2, containers
    for container in containers:
        inspected = subprocess.run(
            ["docker", "inspect", container], capture_output=True, text=True, check=True
        )
        ports = json.loads(inspected.stdout)[0]["NetworkSettings"]["Ports"]
        bindings = [b for published in ports.values() if published for b in published]
        assert bindings, f"{container}: no published port"
        assert {b["HostIp"] for b in bindings} == {HOST}, bindings


CHECKS: list[Callable[[], None]] = [
    check_row_counts,
    check_roles,
    check_published_ports,
    check_catalog_synced,
    check_second_init_changes_nothing,
]


def main() -> int:
    failed = 0
    for check in CHECKS:
        try:
            check()
        except Exception as error:
            failed += 1
            print(f"FAIL {check.__name__}: {error}")
        else:
            print(f"ok   {check.__name__}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
