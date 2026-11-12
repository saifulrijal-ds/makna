"""WP-11 acceptance 1 and 2: the PostgreSQL connector on the sample database, as the read-only
role. The password rule (criterion 2, second half) is a unit test with a mocked connection."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest
import yaml
from psycopg.conninfo import make_conninfo
from sqlglot import exp

from makna.engine.base import EngineError
from makna.engine.postgres import PostgresConnector
from tests.catalog_support import EXPORT


@pytest.fixture
def connector(engine_url: str) -> PostgresConnector:
    return PostgresConnector(engine_url, statement_timeout_seconds=30)


def catalog_columns() -> dict[str, list[str]]:
    """Table -> column names in table order, from the committed catalog export. It is written
    from the generator spec, which also creates the tables, so it is not read from PostgreSQL."""
    data = yaml.safe_load(EXPORT.read_text(encoding="utf-8"))
    return {
        d["name"]: [c["name"] for c in sorted(d["columns"], key=lambda c: c["ordinal"])]
        for d in data["datasets"]
    }


def test_list_tables_returns_the_nine_tables_of_core_with_their_columns(
    connector: PostgresConnector,
) -> None:
    tables = connector.list_tables("core")
    expected = catalog_columns()
    assert len(expected) == 9
    assert {t.name for t in tables} == set(expected)
    for table in tables:
        assert table.schema == "core"
        assert [c.name for c in table.columns] == expected[table.name], table.name
        assert all(c.native_type for c in table.columns)


def test_list_tables_reports_native_types(connector: PostgresConnector) -> None:
    by_name = {
        t.name: {c.name: c.native_type for c in t.columns} for t in connector.list_tables("core")
    }
    assert by_name["contract_monthly_snapshots"]["snapshot_date"] == "date"
    assert by_name["contracts"]["contract_id"] in ("integer", "bigint")
    assert str(by_name["contracts"]["principal_amount"]).startswith("numeric")


def test_list_tables_of_an_unknown_schema_is_empty(connector: PostgresConnector) -> None:
    assert connector.list_tables("no_such_schema") == []


def test_latest_value_of_the_snapshot_date_is_the_last_month_end(
    connector: PostgresConnector,
) -> None:
    column = exp.column("snapshot_date")
    assert connector.latest_value("core.contract_monthly_snapshots", column) == date(2026, 6, 30)
    # The three-part name the semantic layer uses resolves to the same table.
    assert connector.latest_value("lending.core.contract_monthly_snapshots", column) == date(
        2026, 6, 30
    )


def test_latest_value_of_nothing_is_none(connector: PostgresConnector) -> None:
    never = exp.Case(
        ifs=[
            exp.If(
                this=exp.GT(
                    this=exp.column("snapshot_date"),
                    expression=exp.cast(exp.Literal.string("2999-01-01"), "date"),
                ),
                true=exp.column("snapshot_date"),
            )
        ]
    )
    assert connector.latest_value("core.contract_monthly_snapshots", never) is None


def test_the_connection_is_read_only_and_carries_the_timeout(engine_url: str) -> None:
    connector = PostgresConnector(engine_url, statement_timeout_seconds=7)
    with connector._connect() as connection:
        assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
        assert connection.execute("SHOW statement_timeout").fetchone() == ("7s",)


def test_a_query_over_the_statement_timeout_raises_a_clear_error(engine_url: str) -> None:
    connector = PostgresConnector(engine_url, statement_timeout_seconds=1)
    slow = exp.cast(exp.Anonymous(this="pg_sleep", expressions=[exp.Literal.number(5)]), "text")
    with pytest.raises(EngineError) as info:
        connector.latest_value("core.branches", slow)
    message = str(info.value)
    assert "statement timeout of 1 seconds" in message
    assert "statement_timeout_seconds" in message


def test_a_failed_connection_names_the_target_and_never_the_password(engine_url: str) -> None:
    url = make_conninfo(engine_url, dbname="wp11_no_such_database")
    with pytest.raises(EngineError) as info:
        PostgresConnector(url).list_tables("core")
    message = str(info.value)
    assert "Cannot connect to the engine" in message and "wp11_no_such_database" in message
    password = str(psycopg.conninfo.conninfo_to_dict(engine_url).get("password") or "")
    if password:
        assert password not in message
