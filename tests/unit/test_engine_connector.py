"""WP-11 criterion 2 (password), and the connector's behaviour that needs no database."""

from __future__ import annotations

import logging
from typing import Any

import psycopg
import pytest
from sqlglot import exp

from makna.engine.base import EngineError, redact_url, scrub
from makna.engine.postgres import PostgresConnector

PASSWORD = "s3cr3t-Pa55word"
URL = f"postgresql://lending_reader:{PASSWORD}@db.internal:5432/lending"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (URL, "postgresql://lending_reader@db.internal:5432/lending"),
        ("postgres://u:p%40ss@h/d", "postgres://u@h/d"),
        ("postgresql://u@h/d", "postgresql://u@h/d"),
        ("postgresql://h/d", "postgresql://h/d"),
        ("postgresql://u:pw@/d?host=/tmp/pg&password=zz&sslmode=require",
         "postgresql://u@/d?host=/tmp/pg&sslmode=require"),
        ("host=db user=u password=pw dbname=lending", "user=u dbname=lending host=db"),
    ],
)  # fmt: skip
def test_redact_url_drops_the_password(url: str, expected: str) -> None:
    assert redact_url(url) == expected


@pytest.mark.parametrize("url", ["", "not a url", "postgresql://u:pw@[bad/d", "host=db password"])
def test_redact_url_never_returns_what_it_cannot_read_as_it_was(url: str) -> None:
    assert "pw" not in redact_url(url).replace("password", "")


def test_scrub_replaces_the_password_wherever_it_appears() -> None:
    text = f"could not connect with {PASSWORD}; again {PASSWORD}"
    assert PASSWORD not in scrub(text, URL)
    assert scrub("no secret here", URL) == "no secret here"
    assert scrub(text, "postgresql://u@h/d") == text


def connector(**kwargs: Any) -> PostgresConnector:
    return PostgresConnector(URL, **kwargs)


def test_the_target_has_no_password() -> None:
    assert PASSWORD not in connector().target
    assert connector().target.endswith("@db.internal:5432/lending")


def test_a_failed_connection_reaches_neither_the_log_nor_the_exception_with_the_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The mocked driver does the worst case: it repeats the whole conninfo in its message."""

    def refuse(conninfo: str, **kwargs: Any) -> None:
        raise psycopg.OperationalError(f"connection to server failed, conninfo {conninfo}")

    monkeypatch.setattr("makna.engine.postgres.psycopg.connect", refuse)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(EngineError) as info:
            connector().list_tables("core")
    assert "Cannot connect to the engine" in str(info.value)
    assert "db.internal" in str(info.value)
    assert PASSWORD not in str(info.value) and PASSWORD not in repr(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert caplog.records, "a connection failure must be logged"
    for record in caplog.records:
        assert PASSWORD not in record.getMessage()
        assert PASSWORD not in repr(record.args) and PASSWORD not in str(record.exc_info)


def test_a_malformed_url_is_an_engine_error_without_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = PostgresConnector(f"postgresql://u:{PASSWORD}@h:notaport/d")
    with pytest.raises(EngineError) as info:
        bad.list_tables("core")
    assert PASSWORD not in str(info.value)


class FakeCursor:
    def __init__(self, fail: Exception | None, rows: list[tuple[Any, ...]]) -> None:
        self.fail, self.rows = fail, rows
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: str, params: Any = None) -> None:
        self.executed.append((statement, params))
        if self.fail and "set_config" not in statement:
            raise self.fail

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class FakeConnection:
    def __init__(self, fail: Exception | None = None, rows: list[tuple[Any, ...]] | None = None):
        self.cursor_ = FakeCursor(fail, rows or [])
        self.read_only = False
        self.closed = False

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def cursor(self) -> FakeCursor:
        return self.cursor_

    def commit(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def patch_connect(monkeypatch: pytest.MonkeyPatch, connection: FakeConnection) -> list[Any]:
    calls: list[Any] = []

    def connect(conninfo: str, **kwargs: Any) -> FakeConnection:
        calls.append((conninfo, kwargs))
        return connection

    monkeypatch.setattr("makna.engine.postgres.psycopg.connect", connect)
    return calls


def test_a_failed_statement_reaches_neither_the_log_nor_the_exception_with_the_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeConnection(fail=psycopg.errors.InsufficientPrivilege(f"denied for {PASSWORD}"))
    patch_connect(monkeypatch, fake)
    with caplog.at_level(logging.DEBUG), pytest.raises(EngineError) as info:
        connector().list_tables("core")
    assert PASSWORD not in str(info.value)
    assert all(PASSWORD not in r.getMessage() for r in caplog.records)
    assert caplog.records


def test_the_connection_is_read_only_with_the_timeout_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeConnection(rows=[("t", "id", "integer"), ("t", "name", "text")])
    calls = patch_connect(monkeypatch, fake)
    tables = connector(statement_timeout_seconds=7).list_tables("core")
    assert fake.read_only is True
    assert ("SELECT set_config('statement_timeout', %s, false)", ("7000",)) in (
        fake.cursor_.executed
    )
    assert calls[0][0] == URL and calls[0][1]["connect_timeout"] > 0
    assert [(t.schema, t.name, [c.name for c in t.columns]) for t in tables] == [
        ("core", "t", ["id", "name"])
    ]
    assert fake.closed


def test_a_timeout_is_reported_as_one_with_its_length_and_no_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_connect(monkeypatch, FakeConnection(fail=psycopg.errors.QueryCanceled("canceled")))
    with pytest.raises(EngineError, match="statement timeout of 5 seconds") as info:
        connector(statement_timeout_seconds=5).latest_value("core.t", exp.column("d"))
    assert PASSWORD not in str(info.value)


def test_latest_value_builds_a_max_over_the_table_without_the_catalog_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date, datetime

    fake = FakeConnection(rows=[(datetime(2026, 6, 30, 23, 0),)])
    patch_connect(monkeypatch, fake)
    value = connector().latest_value("lending.core.snapshots", exp.column("snapshot_date"))
    assert value == date(2026, 6, 30)
    statements = [s for s, _ in fake.cursor_.executed if "set_config" not in s]
    assert statements == ['SELECT MAX(snapshot_date) FROM "core"."snapshots"']


@pytest.mark.parametrize(
    ("rows", "expected"), [([(None,)], None), ([], None)], ids=["null", "no row"]
)
def test_latest_value_of_nothing_is_none(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple[Any, ...]], expected: None
) -> None:
    patch_connect(monkeypatch, FakeConnection(rows=rows))
    assert connector().latest_value("core.t", exp.column("d")) is expected


def test_latest_value_refuses_a_value_that_is_not_a_date(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_connect(monkeypatch, FakeConnection(rows=[("2026-06-30",)]))
    with pytest.raises(EngineError, match="not a date"):
        connector().latest_value("core.t", exp.column("d"))


def test_latest_value_does_not_change_the_expression_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_connect(monkeypatch, FakeConnection(rows=[(None,)]))
    column = exp.column("d")
    connector().latest_value("core.t", column)
    assert column.parent is None


@pytest.mark.parametrize("table", ["", "t", "a.b.c.d", "core.", ".t"])
def test_a_table_name_that_is_not_schema_table_is_refused(table: str) -> None:
    with pytest.raises(EngineError, match="schema.table"):
        connector().latest_value(table, exp.column("d"))


def test_a_timeout_below_one_second_is_refused() -> None:
    with pytest.raises(EngineError, match="at least 1 second"):
        connector(statement_timeout_seconds=0)
