"""WP-17 (WEB-1): the freshness check's pure parts, the page texts, and the command without its
settings. The database and engine parts are in `tests/integration/test_freshness.py`."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlglot import exp

from makna.cli.main import main
from makna.compiler.snapshot import Model
from makna.definitions.loader import official_objects
from makna.services.discovery import Freshness, combine
from makna.services.freshness import measure_checks, measure_id, time_expression
from makna.web.discovery import (
    NOT_AVAILABLE,
    ago,
    bench_path,
    freshness_text,
    page_path,
    valid_name,
)
from tests.catalog_support import DEFINITIONS, make_entity

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def sample_model() -> Model:
    found = official_objects(DEFINITIONS)
    return Model.build(found.entities, [])


def test_every_sample_measure_gets_one_check_with_an_unqualified_quoted_column() -> None:
    model = sample_model()
    checks, failures = measure_checks(model)
    assert failures == []
    wanted = {
        measure_id(entity.name, measure.name)
        for entity in model.entities.values()
        for measure in entity.measures or []
    }
    assert {c.object_id for c in checks} == wanted and len(checks) == len(wanted) > 10
    one = next(
        c for c in checks if c.object_id == "measure:contract_snapshot.outstanding_principal"
    )
    assert one.table == "lending.core.contract_monthly_snapshots"
    assert one.time_expr.sql(dialect="postgres") == '"snapshot_date"'


def test_a_time_dimension_with_an_expression_is_parsed_into_a_tree_without_a_table() -> None:
    entity = make_entity(
        "thing",
        dimensions=[
            {
                "name": "booked",
                "type": "time",
                "expr": "CAST(t.booked_at AS DATE)",
                "description": "When the row was booked, as a date.",
            }
        ],
        measures=[
            {
                "name": "row_count",
                "agg": "count",
                "time_dimension": "booked",
                "description": "The number of rows booked.",
            }
        ],
    )
    tree = time_expression(entity, (entity.measures or [])[0])
    assert isinstance(tree, exp.Expr)
    assert tree.sql(dialect="postgres") == 'CAST("booked_at" AS DATE)'


def test_a_measure_whose_time_dimension_cannot_be_read_is_a_failure_by_class_only() -> None:
    entity = make_entity(
        "thing",
        dimensions=[
            {"name": "booked", "type": "time", "expr": "CAST(booked_at AS", "description": "x" * 12}
        ],
        measures=[
            {
                "name": "row_count",
                "agg": "count",
                "time_dimension": "booked",
                "description": "x" * 12,
            }
        ],
    )
    checks, failures = measure_checks(Model.build([entity], []))
    assert checks == []
    assert [(f.object_id, f.error_class) for f in failures] == [
        ("measure:thing.row_count", failures[0].error_class)
    ]
    assert "booked_at" not in repr(failures)


def row(object_id: str, latest: date | None, checked: datetime) -> SimpleNamespace:
    return SimpleNamespace(object_id=object_id, latest_value=latest, checked_at=checked)


def test_a_metric_takes_the_earliest_date_of_its_measures_and_the_oldest_check() -> None:
    ids = ["measure:a.x", "measure:b.y"]
    older = NOW - timedelta(hours=5)
    found = combine(
        ids, [row(ids[0], date(2026, 6, 30), NOW), row(ids[1], date(2026, 5, 31), older)]
    )
    assert found == Freshness(date(2026, 5, 31), older, 2)


def test_a_metric_has_no_freshness_while_a_measure_has_no_date() -> None:
    ids = ["measure:a.x", "measure:b.y"]
    assert combine(ids, [row(ids[0], date(2026, 6, 30), NOW)]).latest is None
    both = [row(ids[0], date(2026, 6, 30), NOW), row(ids[1], None, NOW)]
    assert combine(ids, both) == Freshness(measures=2)
    assert combine([], []) == Freshness(measures=0)


def test_the_freshness_line_is_the_date_in_iso_form_and_how_long_ago_it_was_checked() -> None:
    line = freshness_text(Freshness(date(2026, 6, 30), NOW - timedelta(hours=2), 1), NOW)
    assert line == "data up to 2026-06-30, checked 2 hours ago"
    assert freshness_text(Freshness(), NOW) == NOT_AVAILABLE == "freshness not available"
    several = freshness_text(Freshness(date(2026, 6, 30), NOW, 3), NOW)
    assert several == "data up to 2026-06-30, checked just now (the earliest of 3 measures)"
    naive = freshness_text(Freshness(date(2026, 6, 30), datetime(2026, 7, 1, 11, 0), 1), NOW)
    assert naive.endswith("checked 1 hour ago")


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=59), "59 minutes ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(hours=23), "23 hours ago"),
        (timedelta(days=3), "3 days ago"),
        (timedelta(seconds=-30), "just now"),
    ],
)
def test_ago_names_the_largest_whole_unit(delta: timedelta, text: str) -> None:
    assert ago(NOW - delta, NOW) == text


def test_links_are_built_from_checked_names_and_encoded_questions() -> None:
    assert page_path("metric:npf_ratio") == "/metrics/npf_ratio"
    assert page_path("entity:contract") == "/entities/contract"
    assert page_path("glossary_term:npf") == "/glossary/npf"
    assert page_path("domain:lending") == "/domains/lending"
    assert page_path("dimension:contract.region") == "/entities/contract"
    assert page_path("rule:contract_snapshot.in_book") == "/entities/contract_snapshot"
    for bad in ("column:a.b", "catalog_term:x", "metric:../x", "metric:a b", "metric:", "x", ""):
        assert page_path(bad) is None, bad
    assert bench_path('a & "b"?') == "/bench?q=a+%26+%22b%22%3F"
    assert valid_name("metric", "npf_ratio") and valid_name("entity", "a.b-c")
    for bad in ("", "-x", "a/b", "a b", "a\n", "x" * 201):
        assert not valid_name("metric", bad), bad


def test_the_refresh_command_needs_the_engine_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAKNA_ENGINE_URL", raising=False)
    monkeypatch.setenv("MAKNA_DATABASE_URL", "postgresql://u:p@localhost/none")
    assert main(["freshness", "refresh"]) == 2
    err = capsys.readouterr().err
    assert "Missing environment variable: MAKNA_ENGINE_URL" in err


def test_the_refresh_command_needs_the_database_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAKNA_ENGINE_URL", "postgresql://ro:pw@localhost/lending")
    monkeypatch.delenv("MAKNA_DATABASE_URL", raising=False)
    assert main(["freshness", "refresh"]) == 2
    assert "MAKNA_DATABASE_URL" in capsys.readouterr().err


def test_freshness_without_a_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["freshness"]) == 2
    assert "choose refresh" in capsys.readouterr().err
