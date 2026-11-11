"""Checks on a running development stack (CI `stack` job; run it locally after `up -d --wait`).

    uv run python -m deploy.check_stack

Proves WP-05 acceptance 2 (row counts), 4 (a second `init` changes nothing) and 6 (roles and
the published port). Passwords default to the development values of docker-compose.yml.
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
    for step in ("generate-sample-data", "load-lending", "migrate"):
        assert f"init: {step}: unchanged" in output, (
            f"step {step} did not report unchanged:\n{output}"
        )
    time.sleep(2)
    after = snapshot()
    assert before == after, f"state changed:\nbefore={before}\nafter={after}"


def expect_denied(role: str, database: str, statement: str | None = None) -> str:
    try:
        with connect(role, database) as connection:
            if statement is not None:
                connection.execute(statement)
    except psycopg.Error as error:
        return str(error).strip()
    raise AssertionError(f"{role} on {database} was allowed: {statement or 'connect'}")


def check_roles() -> None:
    """Acceptance 6: makna cannot reach lending; the reader can only read it."""
    message = expect_denied("makna", "lending")
    assert "permission denied" in message.lower() or "not allowed" in message.lower(), message
    with connect("makna", "makna") as connection:  # and the role does work on its own database
        assert connection.execute("SELECT count(*) FROM catalog_source").fetchone() == (0,)
    expect_denied("lending_reader", "makna")
    with connect("lending_reader", "lending") as connection:
        assert connection.execute("SELECT count(*) FROM lending.core.branches").fetchone()
    for statement in (
        "INSERT INTO lending.core.branches SELECT * FROM lending.core.branches LIMIT 1",
        "UPDATE lending.core.branches SET branch_name = 'x'",
        "DELETE FROM lending.core.branches",
        "TRUNCATE lending.core.branches",
        "DROP TABLE lending.core.branches",
        "CREATE TABLE lending.core.scratch (a integer)",
        "CREATE TABLE public.scratch (a integer)",
    ):
        expect_denied("lending_reader", "lending", statement)
    # Even a session that switches read-only mode off cannot write.
    with connect("lending_reader", "lending") as connection:
        connection.execute("SET default_transaction_read_only = off")
        try:
            connection.execute("DELETE FROM lending.core.branches")
        except psycopg.errors.InsufficientPrivilege:
            pass
        else:
            raise AssertionError("reader deleted rows")


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
