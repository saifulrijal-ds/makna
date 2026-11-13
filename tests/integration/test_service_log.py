"""WP-12 criterion 6: every service call writes one `tool_call_log` row, including failed and
refused calls, and nothing personal that can be detected is stored."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL

import makna.services.service as service_module
from makna.model.governance import GovernancePolicy
from makna.model.request import QueryError
from makna.services import Caller, PermissionDenied, ServiceError, Services
from makna.services.errors import ServiceErrorCode

from . import service_support as sup
from .service_support import Users

EMAIL = "john.doe@example.com"
CARD = "4111 1111 1111 1111"
PHONE = "+62 812-3456-7890"


def all_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT * FROM tool_call_log ORDER BY id")).mappings().all()
    return [dict(row) for row in rows]


def one_row(engine: Engine, call: Callable[[], object]) -> dict[str, Any]:
    """Run `call` (any outcome) and return the single row it wrote."""
    before = sup.log_count(engine)
    try:
        call()
    except (QueryError, ServiceError):
        pass
    assert sup.log_count(engine) == before + 1
    return dict(sup.last_log(engine))


# --- one row per call, on every outcome ---


def test_every_tool_and_every_outcome_writes_exactly_one_row(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer", channel="web")
    cases: list[tuple[str, Callable[[], object], str, str | None]] = [
        ("search", lambda: svc.search(caller, "kredit macet"), "ok", None),
        ("search", lambda: svc.search(caller, ""), "refused", "INVALID_INPUT"),
        ("get_definition", lambda: svc.get_definition(caller, "metric:npf_ratio"), "ok", None),
        (
            "get_definition",
            lambda: svc.get_definition(caller, "metric:zzz"),
            "refused",
            "NOT_FOUND",
        ),
        ("get_definition", lambda: svc.get_definition(caller, "x"), "refused", "INVALID_INPUT"),
        ("list_related", lambda: svc.list_related(caller, "metric:npf_ratio"), "ok", None),
        ("list_related", lambda: svc.list_related(caller, "metric:zzz"), "refused", "NOT_FOUND"),
        ("compile_query", lambda: svc.compile_query(caller, {"metric": "npf_ratio"}), "ok", None),
        (
            "compile_query",
            lambda: svc.compile_query(caller, {"metric": "nope"}),
            "refused",
            "UNKNOWN_METRIC",
        ),
        ("compile_query", lambda: svc.compile_query(caller, {}), "refused", "INVALID_INPUT"),
    ]
    for tool, call, status, code in cases:
        row = one_row(engine, call)
        assert (row["tool"], row["status"], row["error_code"]) == (tool, status, code)
        assert row["channel"] == "web" and row["user_id"] == users.ids["viewer"]
        assert row["token_id"] is None
        assert isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0
        assert row["at"] is not None


def test_a_token_call_records_the_token_and_its_user_and_channel(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    row = one_row(engine, lambda: sup.services(engine).search(users.token(channel="mcp"), "npf"))
    assert row["token_id"] == users.token_id and row["user_id"] == users.ids["admin"]
    assert row["channel"] == "mcp"


def test_the_result_holds_ids_and_the_confidence_band_never_text(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    row = one_row(engine, lambda: svc.search(caller, "kredit macet"))
    assert row["result"]["confidence"] == "high"
    assert row["result"]["ids"][0] == "metric:npf_ratio"
    stored = json.dumps(row["result"])
    assert "Share of outstanding" not in stored and "label" not in stored
    row = one_row(engine, lambda: svc.compile_query(caller, {"metric": "npf_ratio"}))
    assert row["result"] == {"ids": ["metric:npf_ratio"], "dialect": "postgres"}
    assert "SELECT" not in json.dumps(row, default=str).upper()
    row = one_row(engine, lambda: svc.get_definition(caller, "metric:zzz"))
    assert row["result"] == {"error_code": "NOT_FOUND"}


def test_a_permission_error_is_logged_too(
    service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No role of the sample lacks a read or compile permission, so the denial is made to
    happen the only way it can: the matrix is changed for this test."""
    engine, users = service_world
    from makna.services import permissions

    monkeypatch.setitem(
        permissions.MATRIX[permissions.Permission.COMPILE], "viewer", permissions.Grant.NO
    )
    svc = sup.services(engine)
    with pytest.raises(PermissionDenied) as raised:
        svc.compile_query(users.caller("viewer"), {"metric": "npf_ratio"})
    assert isinstance(raised.value, PermissionError)
    row = dict(sup.last_log(engine))
    assert (row["status"], row["error_code"]) == ("refused", "PERMISSION_DENIED")
    # the other roles still compile
    assert svc.compile_query(users.caller("curator"), {"metric": "npf_ratio"})


def test_an_unexpected_exception_is_internal_with_no_text_and_status_error(
    service_world: tuple[Engine, Users],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, users = service_world
    secret = "customer 3201010101900001 jane@example.com failed"

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError(secret)

    svc = sup.services(engine)
    monkeypatch.setattr(service_module, "compile_request", boom)
    with caplog.at_level(logging.INFO):
        with pytest.raises(ServiceError) as raised:
            svc.compile_query(users.caller("viewer"), {"metric": "npf_ratio"})
    error = raised.value
    assert error.code == ServiceErrorCode.INTERNAL and error.details == {}
    assert "3201" not in error.message and "jane" not in error.message
    assert error.__cause__ is None and error.__suppress_context__
    assert "3201" not in " ".join(r.getMessage() for r in caplog.records)
    row = sup.last_log(engine)
    assert (row["status"], row["error_code"]) == ("error", "INTERNAL")
    assert "3201" not in json.dumps(row, default=str)


# --- a failed log write never changes the answer ---


def test_a_failed_log_write_is_logged_and_the_answer_is_unchanged(
    service_world: tuple[Engine, Users], caplog: pytest.LogCaptureFixture
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    ghost = Caller(user_id=999_999, role="viewer", channel="mcp")  # no such user: the insert fails
    before = sup.log_count(engine)
    with caplog.at_level(logging.ERROR):
        found = svc.search(ghost, "kredit macet")
        with pytest.raises(ServiceError) as raised:
            svc.get_definition(ghost, "metric:zzz")
    assert found.hits and raised.value.code == ServiceErrorCode.NOT_FOUND
    assert sup.log_count(engine) == before  # nothing was stored
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2 and all("tool_call_log write failed" in e for e in errors)
    assert not any("kredit" in e or "999999" in e for e in errors)


# --- redaction (D-24) ---


def test_a_query_with_an_email_a_card_number_and_a_phone_stores_none_of_them(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    query = f"NPF for {EMAIL} card {CARD} phone {PHONE} per wilayah"
    row = one_row(engine, lambda: svc.search(users.caller("viewer"), query))
    stored = json.dumps(row, default=str)
    assert "[REDACTED]" in stored and "NPF for" in stored and "per wilayah" in stored
    forbidden = (
        EMAIL,
        "john",
        "doe",
        "example.com",
        "@",
        "4111",
        "1111",
        "3456",
        "7890",
        "812",
        "+62",
    )
    for piece in forbidden:
        assert piece not in stored, piece
    assert not re.search(r"\d{4}", re.sub(r"\d{4}-\d{2}-\d{2}", "", stored.split('"at"')[0]))


def test_every_tool_redacts_what_the_caller_typed(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    junk = f"{EMAIL} {CARD} {PHONE}"
    rows = [
        one_row(engine, lambda: svc.get_definition(caller, f"metric:{CARD.replace(' ', '')}")),
        one_row(engine, lambda: svc.get_definition(caller, f"metric:{junk}")),
        one_row(engine, lambda: svc.list_related(caller, "metric:x", relation=junk)),
        one_row(engine, lambda: svc.search(caller, "x", domain=junk)),
        one_row(engine, lambda: svc.search(caller, "x", kinds=[junk])),
        one_row(
            engine,
            lambda: svc.compile_query(
                caller,
                {
                    "metric": junk,
                    "dimensions": [junk],
                    "grain": junk,
                    "time_start": junk,
                    "time_end": junk,
                    "filters": [{"dimension": junk, "op": junk, "value": junk}],
                },
            ),
        ),
    ]
    for row in rows:
        stored = json.dumps(row, default=str)
        for piece in ("john", "example.com", "4111", "3456", "7890", "@", "1111111111111111"):
            assert piece not in stored, (row["tool"], piece)


def test_a_filter_value_on_a_personal_dimension_is_never_stored(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    value = "1987-03-14"
    request = {
        "metric": "npf_ratio",
        "dimensions": ["branch.region_name"],
        "filters": [
            {"dimension": "customer.birth_year", "op": "=", "value": value},
            {"dimension": "branch.region_name", "op": "=", "value": "Jawa Barat"},
        ],
    }
    row = one_row(engine, lambda: svc.compile_query(caller, request))
    assert row["error_code"] == "PERSONAL_DATA_REFUSED"
    assert value not in json.dumps(row, default=str) and "Jawa Barat" not in json.dumps(
        row, default=str
    )
    # allowed by a relaxed policy and then compiled successfully: still not stored
    relaxed = sup.services(engine, GovernancePolicy.custom(allow_personal_filters=True))
    row = one_row(engine, lambda: relaxed.compile_query(caller, request))
    assert row["status"] == "ok"
    assert value not in json.dumps(row, default=str)
    stored_filters = row["request"]["filters"]
    assert stored_filters[0] == {
        "dimension": "customer.birth_year",
        "op": "=",
        "value": "[REDACTED]",
    }
    assert stored_filters[1]["value"] == "Jawa Barat"  # a non-personal value is kept


def test_a_value_on_a_dimension_of_an_unknown_metric_or_an_unknown_dimension_is_not_stored(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    for request in (
        {
            "metric": "no_such",
            "filters": [{"dimension": "branch.region_name", "op": "=", "value": "Aceh"}],
        },
        {
            "metric": "npf_ratio",
            "filters": [{"dimension": "customer.shoe", "op": "=", "value": "Aceh"}],
        },
    ):
        row = one_row(engine, partial(svc.compile_query, caller, request))
        assert row["status"] == "refused"
        assert "Aceh" not in json.dumps(row, default=str)
        assert row["request"]["filters"][0]["value"] == "[REDACTED]"


def test_a_value_on_a_catalog_derived_personal_dimension_is_not_stored(
    empty_database: URL,
    tmp_path: Path,
) -> None:
    from sqlalchemy import create_engine

    from ..catalog_support import export_with_tags

    export = export_with_tags(tmp_path / "t.yaml", {("customers", "city"): ["PII.Sensitive"]})
    users = sup.build(empty_database, export=export)
    engine = create_engine(empty_database)
    try:
        svc = sup.services(engine)
        request = {
            "metric": "new_customer_count",
            "filters": [{"dimension": "customer.city", "op": "=", "value": "Bandung"}],
        }
        row = one_row(engine, lambda: svc.compile_query(users.caller("viewer"), request))
        assert row["error_code"] == "BROKEN_REFERENCE"  # R03 is open: the catalog moved ahead
        assert "Bandung" not in json.dumps(row, default=str)
        relaxed = sup.services(engine, GovernancePolicy.custom(allow_personal_filters=True))
        row = one_row(engine, lambda: relaxed.compile_query(users.caller("viewer"), request))
        assert "Bandung" not in json.dumps(row, default=str)
    finally:
        engine.dispose()


def test_a_personal_data_refusal_stores_no_free_text_at_all(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    request = {
        "metric": "npf_ratio",
        "dimensions": ["customer.full_name", f"mail {EMAIL}"],
        "filters": [
            {"dimension": "customer.national_id", "op": "=", "value": "3201010101900001"},
            {"dimension": f"x {PHONE}", "op": "in", "value": [CARD, EMAIL]},
        ],
        "grain": "month",
        "time_start": "2025-01-01",
        "limit": 3,
    }
    row = one_row(engine, lambda: svc.compile_query(users.caller("viewer"), request))
    assert row["error_code"] == "PERSONAL_DATA_REFUSED" and row["status"] == "refused"
    assert row["request"] == {
        "metric": "npf_ratio",
        "dimensions": ["customer.full_name", "[REDACTED]"],
        "filters": [{"dimension": "customer.national_id"}, {"dimension": "[REDACTED]"}],
    }
    assert row["result"] == {"error_code": "PERSONAL_DATA_REFUSED"}
    assert row["tool"] == "compile_query"
    stored = json.dumps(row, default=str)
    for piece in ("3201", "john", "example", "4111", "812", "month", "limit"):
        assert piece not in stored.replace('"limit"', ""), piece


def test_a_personal_data_refusal_for_a_filter_is_the_same(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    request = {
        "metric": "npf_ratio",
        "filters": [{"dimension": "customer.birth_year", "op": "=", "value": "1980-05-05"}],
    }
    row = one_row(
        engine, lambda: sup.services(engine).compile_query(users.caller("viewer"), request)
    )
    assert row["request"] == {
        "metric": "npf_ratio",
        "dimensions": [],
        "filters": [{"dimension": "customer.birth_year"}],
    }


def test_the_log_is_not_a_second_copy_of_the_answers(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    svc.get_definition(users.caller("viewer"), "metric:npf_ratio")
    svc.search(users.caller("viewer"), "npf")
    assert isinstance(svc, Services)
    text_form = json.dumps(all_rows(engine), default=str)
    assert "Share of outstanding principal" not in text_form
