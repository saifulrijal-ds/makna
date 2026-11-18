"""WP-17 acceptance 6 (the part that needs no sample data) and the rulings on freshness: the
refresh stores one row for each approved measure, an engine failure is reported by the measure's id
and the error's class and never by its text, and the page shows what was stored.

The engine part of acceptance 6, "data up to 2026-06-30" read from the sample data, is in
`tests/engine/test_freshness_sample.py`.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import URL, Engine, create_engine, select, text
from sqlglot import exp

from makna.cli.main import main
from makna.engine.base import EngineError
from makna.index.tables import data_freshness
from makna.services.discovery import Discovery
from makna.services.freshness import measure_checks, refresh
from makna.services.snapshot import SnapshotCache

from . import web_support as w

SECRET = "4111 1111 1111 1111 " + secrets.token_hex(4)
SNAPSHOT_TABLE = "lending.core.contract_monthly_snapshots"


class FakeConnector:
    """Answers by table; a table in `fail` raises an `EngineError` whose text holds a secret."""

    dialect = "postgres"

    def __init__(self, dates: dict[str, date | None], fail: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.dates = dates
        self.fail = set(fail)
        self.asked: list[tuple[str, str]] = []

    def list_tables(self, schema: str) -> list[Any]:  # pragma: no cover - not used
        return []

    def latest_value(self, table: str, expr: exp.Expr) -> date | None:
        self.asked.append((table, expr.sql(dialect="postgres")))
        if table in self.fail:
            raise EngineError(f'invalid input syntax for type date: "{SECRET}"')
        return self.dates.get(table)


def stored(engine: Engine) -> dict[str, tuple[date | None, datetime]]:
    with engine.connect() as connection:
        rows = connection.execute(select(data_freshness))
    return {r.object_id: (r.latest_value, r.checked_at) for r in rows}


# --- the refresh ---


def test_the_refresh_stores_one_row_for_every_approved_measure_and_updates_it_next_time(
    fresh_service_world: tuple[Engine, Any],
) -> None:
    engine, _ = fresh_service_world
    snapshot, _ = SnapshotCache(engine).current()
    checks, _ = measure_checks(snapshot.model)
    connector = FakeConnector({SNAPSHOT_TABLE: date(2026, 6, 30)})
    first = refresh(engine, connector)
    assert (first.checked, first.stored, first.failures) == (len(checks), len(checks), ())
    rows = stored(engine)
    assert set(rows) == {c.object_id for c in checks}
    assert rows["measure:contract_snapshot.outstanding_principal"][0] == date(2026, 6, 30)
    assert len(first.empty) == len([c for c in checks if c.table != SNAPSHOT_TABLE])
    # A draft's measure is not in the model: only approved entities are asked about.
    assert all("write_off" not in c.object_id for c in checks)
    # The query of the freshness check is the time dimension's column, quoted, nothing else.
    assert (SNAPSHOT_TABLE, '"snapshot_date"') in connector.asked
    # A second run updates the rows in place.
    connector.dates[SNAPSHOT_TABLE] = date(2026, 7, 31)
    second = refresh(engine, connector, now=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    assert second.stored == len(checks)
    after = stored(engine)
    assert len(after) == len(rows)
    value, checked = after["measure:contract_snapshot.outstanding_principal"]
    assert value == date(2026, 7, 31) and checked == datetime(2030, 1, 1, tzinfo=UTC)


def test_an_engine_error_is_reported_by_measure_and_class_and_the_next_measure_is_checked(
    fresh_service_world: tuple[Engine, Any], caplog: pytest.LogCaptureFixture
) -> None:
    engine, _ = fresh_service_world
    connector = FakeConnector({"lending.core.payments": date(2026, 6, 29)}, fail={SNAPSHOT_TABLE})
    reported: list[Any] = []
    with caplog.at_level(logging.DEBUG):
        result = refresh(engine, connector, on_failure=reported.append)
    failed = {f.object_id for f in result.failures}
    assert "measure:contract_snapshot.outstanding_principal" in failed
    assert {f.error_class for f in result.failures} == {"EngineError"}
    assert reported == list(result.failures)
    assert SECRET not in repr(result) and SECRET not in caplog.text
    # The measures of other tables were still checked and stored; the failed ones were not.
    rows = stored(engine)
    assert any(value == date(2026, 6, 29) for value, _ in rows.values())
    assert failed.isdisjoint(rows)
    assert result.stored == result.checked - len(failed)


def test_a_failed_measure_keeps_its_earlier_row(
    fresh_service_world: tuple[Engine, Any],
) -> None:
    engine, _ = fresh_service_world
    refresh(engine, FakeConnector({SNAPSHOT_TABLE: date(2026, 6, 30)}))
    refresh(engine, FakeConnector({}, fail={SNAPSHOT_TABLE}))
    assert stored(engine)["measure:contract_snapshot.outstanding_principal"][0] == date(2026, 6, 30)


# --- the pages ---


def metric_freshness(client: Any, name: str) -> str:
    html = client.get(f"/metrics/{name}").text
    start = html.index('data-field="freshness"')
    return str(html[start : html.index("</p>", start)])


def test_the_metric_page_shows_what_the_refresh_stored_and_not_available_before(
    fresh_service_world: tuple[Engine, Any],
) -> None:
    engine, _ = fresh_service_world
    app = w.make_app(engine.url)
    try:
        client = w.signed_in(app, w.make_user(engine, "viewer"))
        assert "freshness not available" in metric_freshness(client, "outstanding_principal")
        refresh(engine, FakeConnector({SNAPSHOT_TABLE: date(2026, 6, 30)}))
        line = metric_freshness(client, "outstanding_principal")
        assert "data up to 2026-06-30, checked just now" in line
        # A ratio reads the measures of its inputs: here they all sit in one table.
        assert "data up to 2026-06-30" in metric_freshness(client, "npf_ratio")
    finally:
        app.state.services.dispose()


def test_a_metric_over_two_tables_shows_the_earlier_date_and_says_so(
    fresh_service_world: tuple[Engine, Any],
) -> None:
    engine, _ = fresh_service_world
    snapshot, _ = SnapshotCache(engine).current()
    tables = {
        snapshot.model.entities[e].source.table
        for e in ("payment", "installment")
        if e in snapshot.model.entities
    }
    assert len(tables) == 2
    first, second = sorted(tables)
    app = w.make_app(engine.url)
    try:
        client = w.signed_in(app, w.make_user(engine, "viewer"))
        refresh(engine, FakeConnector({first: date(2026, 6, 30), second: date(2026, 5, 31)}))
        line = metric_freshness(client, "collection_rate")
        assert "data up to 2026-05-31" in line and "the earliest of 2 measures" in line
        # One table without a date, and the metric has none to show.
        refresh(engine, FakeConnector({first: date(2026, 6, 30), second: None}))
        assert "freshness not available" in metric_freshness(client, "collection_rate")
    finally:
        app.state.services.dispose()


def test_a_draft_metric_has_no_freshness_and_a_viewer_cannot_open_it(
    fresh_service_world: tuple[Engine, Any],
) -> None:
    engine, _ = fresh_service_world
    services = w.make_app(engine.url).state.services
    try:
        assert services.discovery.freshness("write_off_amount").latest is None
        assert services.discovery.freshness("no_such_metric").measures == 0
    finally:
        services.dispose()


# --- the command ---


@pytest.fixture
def command_env(fresh_service_world: tuple[Engine, Any], monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine, _ = fresh_service_world
    monkeypatch.setenv(
        "MAKNA_DATABASE_URL",
        engine.url.set(drivername="postgresql").render_as_string(hide_password=False),
    )
    return engine


def test_the_command_uses_the_engine_url_and_reports_a_failure_by_class_only(
    command_env: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeConnector({SNAPSHOT_TABLE: date(2026, 6, 30)}, fail={"lending.core.payments"})
    urls: list[str] = []

    class Connector:
        dialect = "postgres"
        target = "postgresql://reader@host/lending"

        def __init__(self, url: str, **_: Any) -> None:
            urls.append(url)

        def latest_value(self, table: str, expr: exp.Expr) -> date | None:
            # The real connector logs its own message, which holds the engine's text.
            logging.getLogger("makna.engine.postgres").warning("engine said: %s", SECRET)
            return fake.latest_value(table, expr)

    monkeypatch.setattr("makna.cli.main.PostgresConnector", Connector)
    monkeypatch.setenv(
        "MAKNA_ENGINE_URL", "postgresql://reader:pw-" + secrets.token_hex(3) + "@h/db"
    )
    code = main(["freshness", "refresh"])
    captured = capsys.readouterr()
    assert code == 1
    assert urls and urls[0] == os.environ["MAKNA_ENGINE_URL"]
    for text_ in (captured.out, captured.err):
        assert SECRET not in text_
    assert "makna freshness refresh: measure:payment." in captured.err
    assert "failed (EngineError)" in captured.err
    assert "measures checked:" in captured.out and "failed: " in captured.out
    assert "pw-" not in captured.out + captured.err
    assert "measure:contract_snapshot.outstanding_principal" in stored(command_env)


def create_scratch(test_database_url: URL, with_tables: bool) -> Iterator[URL]:
    from data.lending_loader import create_tables

    name = f"lending_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(test_database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = test_database_url.set(database=name)
    try:
        if with_tables:
            with psycopg.connect(plain(url), autocommit=True) as connection:
                create_tables(connection)
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def plain(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def empty_tables(test_database_url: URL) -> Iterator[URL]:
    yield from create_scratch(test_database_url, with_tables=True)


@pytest.fixture
def no_tables(test_database_url: URL) -> Iterator[URL]:
    yield from create_scratch(test_database_url, with_tables=False)


def test_the_command_on_empty_tables_stores_rows_without_a_date(
    command_env: Engine,
    empty_tables: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MAKNA_ENGINE_URL", plain(empty_tables))
    assert main(["freshness", "refresh"]) == 0
    out = capsys.readouterr().out
    rows = stored(command_env)
    assert rows and all(value is None for value, _ in rows.values())
    assert "failed: 0" in out and "no date for measure:contract_snapshot." in out
    app = w.make_app(command_env.url)
    try:
        client = w.signed_in(app, w.make_user(command_env, "viewer"))
        assert "freshness not available" in metric_freshness(client, "outstanding_principal")
    finally:
        app.state.services.dispose()


def test_the_command_with_the_real_connector_never_prints_the_engine_text(
    command_env: Engine,
    no_tables: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MAKNA_ENGINE_URL", plain(no_tables))
    assert main(["freshness", "refresh"]) == 1
    captured = capsys.readouterr()
    shown = captured.out + captured.err
    # The engine says `relation "core.contract_monthly_snapshots" does not exist`.
    assert "does not exist" not in shown and "contract_monthly_snapshots" not in shown
    assert "failed (EngineError)" in captured.err
    assert stored(command_env) == {}


# --- the reads of the pages, at the service level ---


def test_the_page_reads_follow_the_visibility_rules(
    fresh_service_world: tuple[Engine, Any],
) -> None:
    engine, users = fresh_service_world
    services = w.make_app(engine.url).state.services
    try:
        discovery: Discovery = services.discovery
        viewer, curator = users.caller("viewer", "web"), users.caller("curator", "web")
        token = users.token("admin", "mcp")

        def names(listing: Any) -> set[str]:
            return {item.name for items in listing.groups.values() for item in items}

        seen = discovery.domain(viewer, "lending")
        assert seen is not None and "write_off_amount" not in names(seen)
        assert list(seen.groups) == ["metric", "entity", "glossary_term"]
        assert names(discovery.domain(token, "lending")) == names(seen)
        own = discovery.domain(curator, "lending")
        assert own is not None and "write_off_amount" in names(own)
        draft = next(i for i in own.groups["metric"] if i.name == "write_off_amount")
        assert draft.status == "draft"
        assert all(i.status is None for i in seen.groups["metric"])
        assert discovery.domain(viewer, "nothing") is None
        summary = discovery.domains(viewer)
        assert [d.name for d in summary] == ["lending"]
        assert summary[0].counts["metric"] == len(seen.groups["metric"])
        assert discovery.domains(curator)[0].counts["metric"] == summary[0].counts["metric"] + 1
        recent = discovery.recently_approved(curator)
        assert len(recent) == 5 and all(i.status is None for i in recent)
    finally:
        services.dispose()


def test_the_init_step_checks_once_and_then_finds_nothing_to_write(
    fresh_service_world: tuple[Engine, Any], empty_tables: URL
) -> None:
    from deploy import init
    from deploy.init import Context

    engine, _ = fresh_service_world
    context = Context(
        data_dir=Path("unused"),
        seed=1,
        scale=0.05,
        database_url=plain(engine.url),
        lending_loader_url="postgresql://unused",
        engine_url=plain(empty_tables),
    )
    first = init.refresh_freshness_step(context)
    assert first.startswith("checked ") and "WARNING" not in first
    rows = stored(engine)
    assert rows
    # Nothing changed on the engine: the second run writes nothing, `checked_at` stays.
    assert init.refresh_freshness_step(context) == "unchanged"
    assert stored(engine) == rows
    # A failing engine is a warning in the line, not a failure of the step.
    broken = Context(
        data_dir=Path("unused"),
        seed=1,
        scale=0.05,
        database_url=plain(engine.url),
        lending_loader_url="postgresql://unused",
        engine_url="postgresql://nobody:pw-" + secrets.token_hex(3) + "@127.0.0.1:1/none",
    )
    line = init.refresh_freshness_step(broken)
    assert "WARNING: measure:" in line and "failed (EngineError)" in line
    assert "pw-" not in line and "127.0.0.1" not in line
