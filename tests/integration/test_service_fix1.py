"""WP-12 fix round 1: the log row is required (safety I-1), compile never uses a stale snapshot
(I-2), D-30 per table (I-3), and the smaller fixes."""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

import makna.services.snapshot as snapshot_module
from makna.catalog.file import FileConnector
from makna.catalog.sync import sync_catalog
from makna.index import indexer, migrate
from makna.model.governance import STRICT, GovernancePolicy
from makna.model.request import QueryError
from makna.services import ServiceError, Services
from makna.services.calllog import CallLog
from makna.services.errors import ServiceErrorCode

from ..catalog_support import (
    TAG_MAP,
    export_data,
    export_with_tags,
    sample_objects,
    write_export,
)
from . import service_support as sup
from .service_support import Users

CITY = {"metric": "new_customer_count", "dimensions": ["customer.city"]}
NUL = "bad\x00value"
SURROGATE = "bad\ud800value"


def rows_since(engine: Engine, before: int) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT * FROM tool_call_log ORDER BY id OFFSET :n"), {"n": before}
        ).mappings()
        return [dict(row) for row in found]


# --- I-1: the log row is required ----------------------------------------------------------------


@pytest.mark.parametrize("bad", [NUL, SURROGATE, "a\x01b", "a\x85b"])
def test_a_bad_character_in_any_text_is_invalid_input_and_still_has_a_row(
    service_world: tuple[Engine, Users], bad: str
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    calls: list[tuple[str, str, Callable[[], object]]] = [
        ("search", "query", lambda: svc.search(caller, f"npf {bad}")),
        ("get_definition", "id", lambda: svc.get_definition(caller, f"metric:{bad}")),
        ("list_related", "id", lambda: svc.list_related(caller, f"metric:{bad}")),
        (
            "list_related",
            "relation",
            lambda: svc.list_related(caller, "metric:npf_ratio", relation=bad),
        ),
        ("search", "domain", lambda: svc.search(caller, "npf", domain=bad)),
        ("search", "kinds", lambda: svc.search(caller, "npf", kinds=[bad])),
        (
            "get_definition",
            "detail",
            lambda: svc.get_definition(caller, "metric:npf_ratio", detail=bad),
        ),
        (
            "compile_query",
            "filters.0.value",
            lambda: svc.compile_query(
                caller,
                {
                    "metric": "disbursement_amount",
                    "filters": [{"dimension": "product.product_name", "op": "=", "value": bad}],
                },
            ),
        ),
        (
            "compile_query",
            "dimensions.0",
            lambda: svc.compile_query(caller, {"metric": "npf_ratio", "dimensions": [bad]}),
        ),
        (
            "compile_query",
            "time_start",
            lambda: svc.compile_query(caller, {"metric": "npf_ratio", "time_start": bad}),
        ),
        (
            "compile_query",
            "grain",
            lambda: svc.compile_query(caller, {"metric": "npf_ratio", "grain": bad}),
        ),
        ("compile_query", "metric", lambda: svc.compile_query(caller, {"metric": bad})),
    ]
    for tool, field, call in calls:
        before = sup.log_count(engine)
        with pytest.raises(ServiceError) as raised:
            call()
        assert raised.value.code == ServiceErrorCode.INVALID_INPUT, (tool, field)
        assert raised.value.details["field"] == field, (tool, field)
        (row,) = rows_since(engine, before)
        assert (row["tool"], row["status"], row["error_code"]) == (tool, "refused", "INVALID_INPUT")
        stored = json.dumps(row, default=str)
        assert "\\u0000" not in stored and "\\ud800" not in stored
        assert row["request"] != {"unloggable": True}  # redaction replaced the character


def test_a_compile_with_a_bad_character_never_reaches_the_planner(
    service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch
) -> None:
    import makna.services.service as service_module

    engine, users = service_world

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("planned")

    monkeypatch.setattr(service_module, "plan_request", boom)
    with pytest.raises(ServiceError) as raised:
        sup.services(engine).compile_query(
            users.caller("viewer"),
            {
                "metric": "disbursement_amount",
                "filters": [{"dimension": "a.b", "op": "=", "value": NUL}],
            },
        )
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -float("inf")])
def test_a_non_finite_filter_value_is_invalid_filter_and_logged(
    service_world: tuple[Engine, Users], value: float
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    before = sup.log_count(engine)
    request = {
        "metric": "disbursement_amount",
        "filters": [{"dimension": "contract.tenor_months", "op": "=", "value": value}],
    }
    with pytest.raises((QueryError, ServiceError)) as raised:
        svc.compile_query(users.caller("viewer"), request)
    error = raised.value
    assert isinstance(error, (QueryError, ServiceError))
    assert error.code == "INVALID_FILTER", error
    (row,) = rows_since(engine, before)
    assert (row["status"], row["error_code"]) == ("refused", "INVALID_FILTER")


def test_a_request_that_cannot_be_redacted_is_stored_as_unloggable_and_the_call_answers(
    service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch
) -> None:
    import makna.services.calllog as calllog

    engine, users = service_world
    svc = sup.services(engine)
    before = sup.log_count(engine)
    monkeypatch.setattr(calllog, "redact_value", lambda *a, **k: 1 / 0)
    assert svc.search(users.caller("viewer"), "kredit macet").hits
    assert svc.compile_query(users.caller("viewer"), {"metric": "npf_ratio"}).sql
    with pytest.raises(ServiceError):
        svc.get_definition(users.caller("viewer"), "metric:zzz")
    rows = rows_since(engine, before)
    # only the field that cannot be redacted is lost, the others are kept
    assert [r["request"]["query"] for r in rows[:1]] == ["[unloggable]"]
    assert rows[0]["request"]["limit"] == 5
    assert rows[1]["request"]["metric"] == "[unloggable]"
    assert rows[1]["request"]["limit"] == 1000 and rows[1]["request"]["filters"] == []
    assert rows[2]["request"]["id"] == "[unloggable]"
    assert [(r["tool"], r["status"], r["error_code"]) for r in rows] == [
        ("search", "ok", None),
        ("compile_query", "ok", None),
        ("get_definition", "refused", "NOT_FOUND"),
    ]


def test_a_request_the_json_column_refuses_is_stored_again_as_unloggable(
    service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    before = sup.log_count(engine)
    monkeypatch.setattr(
        CallLog, "search_request", staticmethod(lambda *a, **k: {"query": "x\x00y"})
    )
    assert svc.search(users.caller("viewer"), "npf").hits
    (row,) = rows_since(engine, before)
    assert row["request"] == {"unloggable": True} and row["status"] == "ok"


def test_the_row_is_required_for_every_tool(
    service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    monkeypatch.setattr(CallLog, "write", lambda self, call: False)
    for call in (
        lambda: svc.search(caller, "npf"),
        lambda: svc.get_definition(caller, "metric:npf_ratio"),
        lambda: svc.list_related(caller, "metric:npf_ratio"),
        lambda: svc.compile_query(caller, {"metric": "npf_ratio"}),
    ):
        with pytest.raises(ServiceError) as raised:
            call()
        assert raised.value.code == ServiceErrorCode.INTERNAL


# --- I-2: compile never uses a stale snapshot --------------------------------------------------


def slow_build(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    entered, release = threading.Event(), threading.Event()
    real = snapshot_module._build

    def slow(connection: Any, current: Any) -> Any:
        entered.set()
        assert release.wait(20)
        return real(connection, current)

    monkeypatch.setattr(snapshot_module, "_build", slow)
    return entered, release


def compile_while_rebuilding(
    svc: Services, users: Users, entered: threading.Event, release: threading.Event
) -> dict[str, Any]:
    """One thread starts the rebuild and holds it; a second compiles meanwhile."""
    caller = users.caller("viewer")
    holder = threading.Thread(target=lambda: svc.get_definition(caller, "metric:npf_ratio"))
    holder.start()
    assert entered.wait(20)
    outcome: dict[str, Any] = {}

    def compile_it() -> None:
        try:
            outcome["response"] = svc.compile_query(caller, CITY)
        except (QueryError, ServiceError) as error:
            outcome["error"] = error

    compiler = threading.Thread(target=compile_it)
    compiler.start()
    compiler.join(0.5)
    outcome["waited"] = compiler.is_alive()  # it must wait for the rebuild, not use the old one
    release.set()
    compiler.join(20)
    holder.join(20)
    assert not compiler.is_alive() and not holder.is_alive()
    return outcome


def test_compile_waits_for_the_rebuild_after_a_sync_that_tags_a_column(
    fresh_service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine, users = fresh_service_world
    svc = sup.services(engine)
    assert svc.compile_query(users.caller("viewer"), CITY)  # caches the untagged snapshot
    tagged = export_with_tags(tmp_path / "t.yaml", {("customers", "city"): ["PII.Sensitive"]})
    sync_catalog(engine, FileConnector(tagged), tag_map=TAG_MAP)  # no reference check: no R03
    entered, release = slow_build(monkeypatch)
    outcome = compile_while_rebuilding(svc, users, entered, release)
    assert outcome["waited"]
    assert "response" not in outcome, "compiled the newly tagged column raw"
    assert outcome["error"].code == "PERSONAL_DATA_REFUSED"


def test_compile_waits_for_the_rebuild_after_the_first_catalog_sync(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sup.index(migrated_engine)
    users = sup.add_users(migrated_engine)
    svc = sup.services(migrated_engine)
    with pytest.raises(ServiceError) as first:
        svc.compile_query(users.caller("viewer"), CITY)
    assert first.value.code == ServiceErrorCode.CATALOG_NOT_SYNCED  # caches the no-catalog snapshot
    tagged = export_with_tags(tmp_path / "t.yaml", {("customers", "city"): ["PII.Sensitive"]})
    sync_catalog(migrated_engine, FileConnector(tagged), tag_map=TAG_MAP)
    entered, release = slow_build(monkeypatch)
    outcome = compile_while_rebuilding(svc, users, entered, release)
    assert outcome["waited"] and "response" not in outcome
    assert outcome["error"].code == "PERSONAL_DATA_REFUSED"


def test_a_snapshot_carries_the_catalog_state_of_its_own_moment(
    fresh_service_world: tuple[Engine, Users],
) -> None:
    engine, users = fresh_service_world
    svc = sup.services(engine)
    svc.compile_query(users.caller("viewer"), CITY)
    snapshot = svc.snapshots.peek()
    assert snapshot is not None and snapshot.catalog_synced
    assert (
        "lending.core.customers" in snapshot.tables and "lending.core.contracts" in snapshot.tables
    )
    assert snapshot.key.catalog_marker == "1"


def test_a_tag_synced_after_the_index_build_changes_the_key_and_the_stored_dimensions(
    fresh_service_world: tuple[Engine, Users], tmp_path: Path
) -> None:
    """Only the catalog marker moves here: no R03 (no reference check), no new definitions."""
    engine, users = fresh_service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    before = svc.get_definition(caller, "metric:new_customer_count")
    assert "customer.city" in {d.ref for d in before.dimensions}
    first = svc.snapshots.peek()
    assert first is not None
    tagged = export_with_tags(tmp_path / "t.yaml", {("customers", "city"): ["PII.Sensitive"]})
    sync_catalog(engine, FileConnector(tagged), tag_map=TAG_MAP)  # entities=None: no R03
    with engine.connect() as connection:
        assert not connection.execute(
            text("SELECT 1 FROM reference_issue WHERE code = 'R03'")
        ).all()
    after = svc.get_definition(caller, "metric:new_customer_count")
    second = svc.snapshots.peek()
    assert second is not None and second.key != first.key
    assert second.key.definitions_version == first.key.definitions_version
    assert second.key.catalog_marker != first.key.catalog_marker
    assert second.model.derived_sensitivity[("lending.core.customers", "city")] == "personal"
    # the stored facts (city available) are not used: the answer agrees with the compiler
    assert "customer.city" in {d.ref for d in after.unavailable_dimensions}
    with pytest.raises(QueryError) as raised:
        svc.compile_query(caller, CITY)
    assert raised.value.code == "PERSONAL_DATA_REFUSED"


# --- I-3: D-30 per table ------------------------------------------------------------------------


@pytest.fixture
def exports(tmp_path: Path) -> dict[str, Path]:
    tagged = export_with_tags(tmp_path / "tagged.yaml", {("customers", "city"): ["PII.Sensitive"]})
    other = copy.deepcopy(export_data())
    other["source"]["name"] = "other-source"
    other["datasets"] = [
        dict(other["datasets"][0], engine_fqn="other.misc.things", catalog_id="x", name="things")
    ]
    partial = copy.deepcopy(export_data())
    partial["datasets"] = [d for d in partial["datasets"] if d["name"] == "branches"]
    return {
        "tagged": tagged,
        "other": write_export(tmp_path / "other.yaml", other),
        "partial": write_export(tmp_path / "partial.yaml", partial),
    }


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param(["other"], id="another source only"),
        pytest.param(["partial"], id="a partial export"),
        pytest.param(["tagged", "partial"], id="a tagged sync, then a partial export"),
    ],
)
def test_a_catalog_that_does_not_cover_the_tables_a_query_reads_is_refused(
    empty_database: URL, exports: dict[str, Path], steps: list[str]
) -> None:
    migrate.upgrade(empty_database.render_as_string(hide_password=False))
    engine = create_engine(empty_database)
    try:
        sup.index(engine)
        users = sup.add_users(engine)
        for step in steps:
            sync_catalog(engine, FileConnector(exports[step]), tag_map=TAG_MAP)
        svc = sup.services(engine)
        with pytest.raises(ServiceError) as raised:
            svc.compile_query(users.token("admin"), CITY)
        error = raised.value
        assert error.code == ServiceErrorCode.CATALOG_NOT_SYNCED
        assert "lending.core.customers" in error.details["tables"]
        assert "makna catalog sync" in error.message
        row = sup.last_log(engine)
        assert (row["status"], row["error_code"]) == ("refused", "CATALOG_NOT_SYNCED")
        # other services still work
        assert svc.get_definition(users.caller("viewer"), "metric:npf_ratio")
    finally:
        engine.dispose()


def test_the_baseline_with_the_tag_refuses_the_personal_column(
    empty_database: URL, exports: dict[str, Path]
) -> None:
    migrate.upgrade(empty_database.render_as_string(hide_password=False))
    engine = create_engine(empty_database)
    try:
        sup.index(engine)
        users = sup.add_users(engine)
        sync_catalog(engine, FileConnector(exports["tagged"]), tag_map=TAG_MAP)
        with pytest.raises(QueryError) as raised:
            sup.services(engine).compile_query(users.token("admin"), CITY)
        assert raised.value.code == "PERSONAL_DATA_REFUSED"
    finally:
        engine.dispose()


def test_joined_tables_are_checked_too(empty_database: URL, tmp_path: Path) -> None:
    """The metric reads `contracts` and joins `branches`: a catalog without `branches` refuses."""
    data = copy.deepcopy(export_data())
    data["datasets"] = [d for d in data["datasets"] if d["name"] != "branches"]
    migrate.upgrade(empty_database.render_as_string(hide_password=False))
    engine = create_engine(empty_database)
    try:
        sup.index(engine)
        users = sup.add_users(engine)
        sync_catalog(
            engine, FileConnector(write_export(tmp_path / "nb.yaml", data)), tag_map=TAG_MAP
        )
        with pytest.raises(ServiceError) as raised:
            sup.services(engine).compile_query(
                users.caller("viewer"),
                {"metric": "disbursement_amount", "dimensions": ["branch.region_name"]},
            )
        assert raised.value.details["tables"] == ["lending.core.branches"]
        assert sup.services(engine).compile_query(
            users.caller("viewer"), {"metric": "disbursement_amount"}
        )
    finally:
        engine.dispose()


# --- M-4 and M-8 ---------------------------------------------------------------------------------


def test_the_build_stamps_the_catalog_marker_it_read_first(
    fresh_service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sync that commits while the build runs is not claimed as covered by its facts."""
    engine, users = fresh_service_world
    tagged = export_with_tags(tmp_path / "t.yaml", {("customers", "city"): ["PII.Sensitive"]})
    real = indexer.catalog_sensitivity

    def racing(connection: Any) -> Any:
        sync_catalog(engine, FileConnector(tagged), tag_map=TAG_MAP)  # commits meanwhile
        return real(connection)

    monkeypatch.setattr(indexer, "catalog_sensitivity", racing)
    sup.index(engine)
    monkeypatch.undo()
    with engine.connect() as connection:
        stamped = connection.execute(
            text("SELECT value FROM index_state WHERE key = 'derived_catalog_marker'")
        ).scalar_one()
        latest = indexer.catalog_marker(connection)
    assert stamped == "1" and latest == "2"
    # so the facts are not trusted: the answer agrees with the compiler
    svc = sup.services(engine)
    shown = svc.get_definition(users.caller("viewer"), "metric:new_customer_count")
    assert "customer.city" in {d.ref for d in shown.unavailable_dimensions}


def test_stored_dimensions_and_their_basis_are_one_value(
    service_world: tuple[Engine, Users],
) -> None:
    engine, _ = service_world
    with engine.connect() as connection:
        basis = connection.execute(
            text("SELECT derived -> 'basis' FROM semantic_object WHERE id = 'metric:npf_ratio'")
        ).scalar_one()
    assert basis == {
        "governance_fingerprint": STRICT.fingerprint(),
        "definitions_version": basis["definitions_version"],
        "catalog_marker": "1",
    }
    assert basis["definitions_version"]


def test_the_relaxed_policy_is_read_from_the_services_object(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine, GovernancePolicy.custom(allow_unmasked_personal=True))
    shown = svc.get_definition(users.caller("viewer"), "metric:npf_ratio")
    assert not shown.unavailable_dimensions
    assert sample_objects()


# --- fix round 2 ---------------------------------------------------------------------------------


def stored_request(engine: Engine, call: Callable[[], object]) -> dict[str, Any]:
    before = sup.log_count(engine)
    try:
        call()
    except (QueryError, ServiceError):
        pass
    (row,) = rows_since(engine, before)
    request = row["request"]
    assert isinstance(request, dict)
    return request


@pytest.mark.parametrize(
    "junk",
    [
        "%00",
        "%0a%00",
        "x%2500",
        "%252500",
        "\ue000\ue001",
        "\ue0000\ue001",
        "\ue0009\ue001",
        "\U000f0000",
    ],
)
def test_a_caller_cannot_blank_the_stored_request(
    service_world: tuple[Engine, Users], junk: str
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    request = stored_request(engine, lambda: svc.search(caller, f"kredit macet {junk} x", limit=3))
    assert request != {"unloggable": True}
    assert request["query"].startswith("kredit macet") and request["limit"] == 3
    request = stored_request(
        engine,
        lambda: svc.compile_query(
            caller,
            {
                "metric": "disbursement_amount",
                "dimensions": ["product.product_category"],
                "filters": [{"dimension": "product.product_name", "op": "=", "value": f"a{junk}"}],
            },
        ),
    )
    assert request["metric"] == "disbursement_amount"
    assert request["dimensions"] == ["product.product_category"]
    assert request["filters"][0]["dimension"] == "product.product_name"
    assert request["filters"][0]["value"].startswith("a")


def test_double_encoded_personal_data_is_removed(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    request = stored_request(
        engine,
        lambda: svc.search(
            users.caller("viewer"), "john%2540example.com 4111%25201111%25201111%25201111 x"
        ),
    )
    assert "john" not in request["query"] and "4111" not in request["query"]


def test_one_field_that_cannot_be_stored_costs_only_that_field(
    service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services.redact import redact_value as real

    engine, users = service_world

    def picky(value: Any, limit: int = 500) -> Any:
        if isinstance(value, str) and "BOOM" in value:
            raise RuntimeError("no")
        return real(value, limit)

    monkeypatch.setattr("makna.services.calllog.redact_value", picky)
    request = stored_request(
        engine,
        lambda: sup.services(engine).compile_query(
            users.caller("viewer"),
            {"metric": "npf_ratio", "grain": "BOOM", "time_start": "2025-01-01"},
        ),
    )
    assert request["grain"] == "[unloggable]"
    assert request["metric"] == "npf_ratio" and request["time_start"] == "2025-01-01"


def test_a_refusal_with_details_is_internal_when_its_row_cannot_be_written(
    service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    monkeypatch.setattr(CallLog, "write", lambda self, call: False)
    for call in (
        lambda: svc.get_definition(caller, "metric:npf_ratoi"),  # NOT_FOUND with close ids
        lambda: svc.compile_query(caller, {"metric": "npf_ratio", "dimensions": ["x.y"]}),
        lambda: svc.search(caller, ""),
        lambda: svc.compile_query(caller, {"metric": "nope"}),
    ):
        with pytest.raises(ServiceError) as raised:
            call()
        assert raised.value.code == ServiceErrorCode.INTERNAL
        assert raised.value.details == {} and "npf" not in raised.value.message


def test_get_definition_waits_for_a_rebuild_and_shows_no_values_of_a_newly_personal_dimension(
    fresh_service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine, users = fresh_service_world
    svc = sup.services(engine, GovernancePolicy.custom(allow_unmasked_personal=True))
    caller = users.caller("viewer")
    assert svc.get_definition(caller, "dimension:customer.gender").values == ["F", "M"]
    tagged = export_with_tags(tmp_path / "t.yaml", {("customers", "gender"): ["PII.Sensitive"]})
    sync_catalog(engine, FileConnector(tagged), tag_map=TAG_MAP)
    entered, release = slow_build(monkeypatch)
    holder = threading.Thread(target=lambda: svc.get_definition(caller, "metric:npf_ratio"))
    holder.start()
    assert entered.wait(20)
    outcome: dict[str, Any] = {}

    def read() -> None:
        outcome["view"] = svc.get_definition(caller, "dimension:customer.gender")

    reader = threading.Thread(target=read)
    reader.start()
    reader.join(0.5)
    assert reader.is_alive()  # it waits; it is not served the old snapshot's values
    release.set()
    reader.join(20)
    holder.join(20)
    assert outcome["view"].values == []
