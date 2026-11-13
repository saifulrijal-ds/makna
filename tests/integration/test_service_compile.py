"""WP-12 criterion 5 (`compile_query`), the refusal without a catalog (D-30), the structured
refusal of a personal column the catalog tags (WP-10 M-3, `R05`), and that `get_definition` and
`compile_query` agree on what is available under the running policy (D-29)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

import makna.services.service as service_module
from makna.catalog.file import FileConnector
from makna.catalog.references import open_issues
from makna.catalog.sync import sync_catalog
from makna.compiler.governance import GovernanceError
from makna.model.governance import GovernancePolicy
from makna.model.request import QueryError
from makna.services import ServiceError
from makna.services.errors import ServiceErrorCode

from ..catalog_support import TAG_MAP, export_data, export_with_tags, sample_objects, write_export
from . import service_support as sup
from .service_support import Users

REGION = {"metric": "npf_ratio", "dimensions": ["branch.region_name"], "grain": "quarter"}


def refusal(call: Any) -> QueryError | ServiceError:
    with pytest.raises((QueryError, ServiceError)) as raised:
        call()
    error = raised.value
    assert isinstance(error, (QueryError, ServiceError))
    return error


# --- criterion 5 ---


def test_a_request_compiles_to_sql_for_the_configured_dialect(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    response = sup.services(engine).compile_query(users.caller("viewer"), REGION)
    assert response.dialect == "postgres"
    assert response.sql.lstrip().upper().startswith("WITH")
    assert [(c.name, c.role) for c in response.columns] == [
        ("metric_time", "time"),
        ("branch__region_name", "dimension"),
        ("npf_outstanding_principal", "input"),
        ("outstanding_principal", "input"),
        ("npf_ratio", "metric"),
    ]
    assert response.explanation and response.metric.id == "metric:npf_ratio"
    assert response.metric.version == 1


def test_every_caller_may_compile_and_a_token_gets_the_same_answer(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    answers = {
        svc.compile_query(caller, REGION).sql
        for caller in (
            *(users.caller(role) for role in sup.ROLES),
            users.token("admin"),
            users.token("viewer"),
        )
    }
    assert len(answers) == 1


def test_the_service_accepts_a_request_model_or_a_mapping(
    service_world: tuple[Engine, Users],
) -> None:
    from makna.model.request import QueryRequest

    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    assert (
        svc.compile_query(caller, QueryRequest(**REGION)).sql
        == svc.compile_query(caller, REGION).sql
    )


def test_a_compiler_error_passes_through_with_its_own_code_and_details(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    error = refusal(
        lambda: svc.compile_query(caller, {"metric": "npf_ratio", "dimensions": ["x.y"]})
    )
    assert isinstance(error, QueryError) and error.code == "UNKNOWN_DIMENSION"
    assert "branch.region_name" in error.details["available_dimensions"]
    error = refusal(lambda: svc.compile_query(caller, {"metric": "npf_ratoi"}))
    assert error.code == "UNKNOWN_METRIC" and error.details["close_names"][0] == "npf_ratio"
    error = refusal(lambda: svc.compile_query(caller, {"metric": "npl_ratio"}))
    assert error.code == "DEPRECATED" and error.details["replaced_by"] == "metric:npf_ratio"
    error = refusal(lambda: svc.compile_query(caller, {**REGION, "grain": "day"}))
    assert error.code == "INVALID_GRAIN"
    row = sup.last_log(engine)
    assert (row["status"], row["error_code"]) == ("refused", "INVALID_GRAIN")


def test_a_draft_is_not_approved_for_a_curator_and_unknown_for_everyone_else(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    request = {"metric": "write_off_amount"}
    for role in ("curator", "approver", "admin"):
        error = refusal(lambda role=role: svc.compile_query(users.caller(role), request))
        assert error.code == "NOT_APPROVED" and error.details == {
            "metric": "write_off_amount",
            "status": "draft",
        }
    for caller in (users.caller("viewer"), users.token("admin")):
        error = refusal(lambda caller=caller: svc.compile_query(caller, request))
        assert error.code == "UNKNOWN_METRIC"
        assert "write_off_amount" not in error.details["close_names"]


def test_invalid_requests_are_invalid_input_naming_the_fields(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    for request, fields in (
        ({}, ["metric"]),
        ({"metric": "npf_ratio", "sql": "select 1"}, ["sql"]),
        ({"metric": "npf_ratio", "dimensions": "branch.region_name"}, ["dimensions"]),
        ({"metric": "npf_ratio", "filters": [{"dimension": "a.b"}]}, ["filters.0.op"]),
        ({"metric": 5}, ["metric"]),
    ):
        error = refusal(lambda request=request: svc.compile_query(caller, request))
        assert isinstance(error, ServiceError) and error.code == ServiceErrorCode.INVALID_INPUT
        assert error.details["fields"] == fields
        assert "select 1" not in error.message
    error = refusal(lambda: svc.compile_query(caller, "select 1"))  # type: ignore[arg-type]
    assert error.code == ServiceErrorCode.INVALID_INPUT


def test_no_service_runs_or_accepts_sql(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    error = refusal(
        lambda: sup.services(engine).compile_query(
            users.caller("viewer"), {"metric": "npf_ratio", "query_sql": "select 1"}
        )
    )
    assert error.code == ServiceErrorCode.INVALID_INPUT


# --- D-30: no catalog, no compile ---


def test_without_a_synced_catalog_compiling_is_refused_and_everything_else_works(
    migrated_engine: Engine,
) -> None:
    from makna.definitions.loader import definition_objects, load_definitions
    from makna.index import indexer
    from makna.search.embedding import HashProvider
    from makna.services.derive import make_derive

    from ..catalog_support import DEFINITIONS

    indexer.build(
        migrated_engine,
        HashProvider(),
        definition_objects(load_definitions(DEFINITIONS)),
        derive=make_derive(GovernancePolicy.strict()),
    )
    users = sup.add_users(migrated_engine)
    svc = sup.services(migrated_engine)
    caller = users.caller("viewer")
    error = refusal(lambda: svc.compile_query(caller, REGION))
    assert isinstance(error, ServiceError) and error.code == ServiceErrorCode.CATALOG_NOT_SYNCED
    assert "makna catalog sync" in error.message
    assert error.details == {}
    row = sup.last_log(migrated_engine)
    assert (row["status"], row["error_code"]) == ("refused", "CATALOG_NOT_SYNCED")
    assert svc.search(caller, "kredit macet").hits
    assert svc.get_definition(caller, "metric:npf_ratio").dimensions
    assert svc.list_related(caller, "metric:npf_ratio").related


def test_a_catalog_whose_datasets_are_all_gone_is_not_a_catalog(
    fresh_service_world: tuple[Engine, Users], tmp_path: Path
) -> None:
    engine, users = fresh_service_world
    svc = sup.services(engine)
    assert svc.compile_query(users.caller("viewer"), REGION)
    empty = export_data()
    empty["datasets"] = []
    empty["glossary_terms"] = []
    sync_catalog(
        engine, FileConnector(write_export(tmp_path / "empty.yaml", empty)), tag_map=TAG_MAP
    )
    error = refusal(lambda: svc.compile_query(users.caller("viewer"), REGION))
    assert error.code == ServiceErrorCode.CATALOG_NOT_SYNCED


# --- WP-10 M-3: a personal column the catalog tags becomes a structured refusal ---

TAGS = {
    ("contracts", "customer_id"): ["PII.Sensitive"],  # a join key
    ("contracts", "disbursement_date"): ["PII.Sensitive"],  # the date of a measure filter
}


@pytest.fixture
def tagged_world(empty_database: URL, tmp_path: Path):  # type: ignore[no-untyped-def]
    """The sample world where the catalog tags a join key and a measure-filter date personal.
    The catalog was synced without the definitions, so R03 is not open: the compiler's final
    check is what stands between the request and the SQL."""
    export = export_with_tags(tmp_path / "tagged.yaml", TAGS)
    users = sup.build(empty_database, export=export, check_references=False)
    engine = create_engine(empty_database)
    yield engine, users
    engine.dispose()


def test_a_personal_join_key_is_refused_at_request_time_with_r05(
    tagged_world: tuple[Engine, Users], caplog: pytest.LogCaptureFixture
) -> None:
    engine, users = tagged_world
    svc = sup.services(engine)
    request = {"metric": "active_contract_count", "dimensions": ["customer.city"]}
    with caplog.at_level(logging.INFO):
        error = refusal(lambda: svc.compile_query(users.caller("viewer"), request))
    assert isinstance(error, QueryError) and error.code == "BROKEN_REFERENCE"
    (issue,) = error.details["issues"]
    assert issue["code"] == "R05" and issue["object"] == "metric:active_contract_count"
    assert "catalog marks column customer_id of table lending.core.contracts" in issue["message"]
    assert "curator must change the definition or the catalog tags" in error.message
    row = sup.last_log(engine)
    assert (row["status"], row["error_code"]) == ("refused", "BROKEN_REFERENCE")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("customer_id" in w and "lending.core.contracts" in w for w in warnings)
    assert not any("internal error" in w or "select" in w.lower() for w in warnings)


def test_the_indexer_opens_r05_for_a_measure_filter_that_reads_a_personal_date(
    tagged_world: tuple[Engine, Users],
) -> None:
    engine, users = tagged_world
    with engine.connect() as connection:
        issues = [i for i in open_issues(connection) if i.code == "R05"]
    assert {i.object_id for i in issues} == {
        "metric:disbursement_amount",
        "metric:disbursed_contract_count",
        "metric:average_ticket_size",
    }
    for issue in issues:
        assert issue.part == "column:lending.core.contracts.disbursement_date"
        assert "lending.core.contracts" in issue.message and "disbursement_date" in issue.message
    svc = sup.services(engine)
    shown = svc.get_definition(users.caller("viewer"), "metric:disbursement_amount")
    assert [i.code for i in shown.issues] == ["R05"]


def test_compiling_such_a_metric_is_refused_before_generation(
    tagged_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, users = tagged_world
    svc = sup.services(engine)

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the compiler must not run for a metric with an open R05 issue")

    monkeypatch.setattr(service_module, "plan_request", boom)
    for metric in ("disbursement_amount", "average_ticket_size"):
        error = refusal(
            lambda metric=metric: svc.compile_query(users.caller("viewer"), {"metric": metric})
        )
        assert isinstance(error, QueryError) and error.code == "BROKEN_REFERENCE"
        assert error.details["issues"][0]["code"] == "R05"
        assert sup.last_log(engine)["status"] == "refused"
    # a metric that does not read the date is not affected
    monkeypatch.undo()
    assert svc.compile_query(users.caller("viewer"), {"metric": "outstanding_principal"})


def test_the_request_time_check_catches_what_the_index_has_not_seen_yet(
    tagged_world: tuple[Engine, Users], caplog: pytest.LogCaptureFixture
) -> None:
    """The catalog moved ahead of the index: no R05 is open, and the final check says so."""
    engine, users = tagged_world
    with engine.begin() as connection:
        connection.execute(text("UPDATE reference_issue SET closed_at = now() WHERE code = 'R05'"))
    svc = sup.services(engine)
    with caplog.at_level(logging.WARNING):
        error = refusal(
            lambda: svc.compile_query(users.caller("viewer"), {"metric": "disbursement_amount"})
        )
    assert isinstance(error, QueryError) and error.code == "BROKEN_REFERENCE"
    (issue,) = error.details["issues"]
    assert issue["code"] == "R05" and issue["object"] == "metric:disbursement_amount"
    assert "column disbursement_date of table lending.core.contracts" in issue["message"]
    assert any(
        "disbursement_date" in r.getMessage() and "lending.core.contracts" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


def test_an_input_with_an_open_r05_breaks_the_metric_built_on_it(
    tagged_world: tuple[Engine, Users],
) -> None:
    engine, users = tagged_world
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO reference_issue (object_id, part, code, message) VALUES "
                "('metric:npf_outstanding_principal', 'column:x.y.z', 'R05', 'a test issue')"
            )
        )
    error = refusal(
        lambda: sup.services(engine).compile_query(users.caller("viewer"), {"metric": "npf_ratio"})
    )
    assert error.code == "BROKEN_REFERENCE"
    assert error.details["issues"][0]["object"] == "metric:npf_outstanding_principal"


def test_a_later_catalog_sync_leaves_r05_alone_and_a_rebuild_closes_it(
    tagged_world: tuple[Engine, Users], tmp_path: Path
) -> None:
    engine, users = tagged_world
    entities, terms = sample_objects()
    plain = write_export(tmp_path / "plain.yaml", export_data())
    # Sync the untagged catalog with the reference check: R03 and the tags go, R05 stays open
    # until the index is built again (it is the indexer's issue, not the catalog check's).
    sync_catalog(
        engine, FileConnector(plain), tag_map=TAG_MAP, entities=entities, glossary_terms=terms
    )
    with engine.connect() as connection:
        codes = {i.code for i in open_issues(connection)}
    assert "R05" in codes
    result = sup.index(engine)
    assert result.issues_closed == 3
    with engine.connect() as connection:
        assert not [i for i in open_issues(connection) if i.code == "R05"]
    assert sup.services(engine).compile_query(
        users.caller("viewer"), {"metric": "disbursement_amount"}
    )


def test_a_governance_error_that_names_no_column_is_internal_and_leaks_nothing(
    service_world: tuple[Engine, Users],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, users = service_world

    def boom(*args: object, **kwargs: object) -> None:
        raise GovernanceError(
            "internal error: SELECT national_id FROM customers; value 3201010101900001"
        )

    monkeypatch.setattr(service_module, "plan_request", boom)
    with caplog.at_level(logging.INFO):
        error = refusal(lambda: sup.services(engine).compile_query(users.caller("viewer"), REGION))
    assert isinstance(error, ServiceError) and error.code == ServiceErrorCode.INTERNAL
    assert error.details == {} and "national_id" not in error.message
    assert "3201" not in " ".join(r.getMessage() for r in caplog.records)
    row = sup.last_log(engine)
    assert (row["status"], row["error_code"]) == ("error", "INTERNAL")


# --- D-29: the policy is the server's, and the two services agree under it ---

POLICIES = {
    "strict": GovernancePolicy.strict(),
    "strict3": GovernancePolicy.strict(3),
    "custom_unmasked": GovernancePolicy.custom(
        allow_unmasked_personal=True, allow_personal_filters=True
    ),
    "custom_no_masks": GovernancePolicy.custom(apply_masks=False, min_group_size=0),
}
SAMPLED = (
    "disbursement_amount",
    "outstanding_principal",
    "npf_ratio",
    "collection_rate",
    "new_customer_count",
    "approval_rate",
)


def all_dimension_refs(svc: Any) -> list[str]:
    snapshot, _ = svc.snapshots.current()
    return [f"{e.name}.{d.name}" for e in snapshot.model.entities.values() for d in e.dimensions]


@pytest.mark.parametrize("name", sorted(POLICIES))
def test_get_definition_and_compile_agree_on_what_is_available(
    tmp_path: Path, empty_database: URL, name: str
) -> None:
    policy = POLICIES[name]
    users = sup.build(empty_database, policy=policy)
    engine = create_engine(empty_database)
    try:
        svc = sup.services(engine, policy)
        caller = users.caller("viewer")
        refs = all_dimension_refs(svc)
        for metric in SAMPLED:
            shown = svc.get_definition(caller, f"metric:{metric}")
            available = {d.ref for d in shown.dimensions}
            unavailable = {d.ref for d in shown.unavailable_dimensions}
            for ref in refs:
                try:
                    svc.compile_query(caller, {"metric": metric, "dimensions": [ref]})
                    outcome = "compiles"
                except QueryError as error:
                    outcome = str(error.code)
                if ref in available:
                    assert outcome not in (
                        "DIMENSION_NOT_AVAILABLE",
                        "UNKNOWN_DIMENSION",
                    ), (name, metric, ref, outcome)
                elif ref in unavailable:
                    assert outcome == "PERSONAL_DATA_REFUSED", (name, metric, ref, outcome)
                else:
                    assert outcome == "DIMENSION_NOT_AVAILABLE", (name, metric, ref, outcome)
    finally:
        engine.dispose()


def test_stored_and_computed_dimensions_are_the_same_answer(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    caller = users.caller("viewer")
    stored = sup.services(engine)
    first = {m: stored.get_definition(caller, f"metric:{m}").to_dict() for m in SAMPLED}
    with engine.begin() as connection:  # the stored facts no longer match the catalog marker
        connection.execute(
            text(
                "UPDATE semantic_object SET derived = jsonb_set(derived, '{basis,catalog_marker}', "
                "'\"x\"') WHERE derived ? 'basis'"
            )
        )
    try:
        computed = sup.services(engine)
        second = {m: computed.get_definition(caller, f"metric:{m}").to_dict() for m in SAMPLED}
    finally:
        sup.index(engine)  # stores the facts again
    assert first == second


def test_another_policy_than_the_stored_one_is_computed_and_warned_about_once(
    service_world: tuple[Engine, Users], caplog: pytest.LogCaptureFixture
) -> None:
    engine, users = service_world
    relaxed = GovernancePolicy.custom(allow_unmasked_personal=True)
    svc = sup.services(engine, relaxed)
    caller = users.caller("viewer")
    with caplog.at_level(logging.WARNING):
        first = svc.get_definition(caller, "metric:npf_ratio")
        svc.get_definition(caller, "metric:outstanding_principal")
        svc.get_definition(caller, "metric:npf_ratio")
    warnings = [r for r in caplog.records if "another governance policy" in r.getMessage()]
    assert len(warnings) == 1 and "makna index build" in warnings[0].getMessage()
    # The running policy decides, not the stored one: nothing is unavailable under `allow_unmasked`.
    assert not first.unavailable_dimensions
    assert "customer.full_name" in {d.ref for d in first.dimensions}
    strict = sup.services(engine).get_definition(caller, "metric:npf_ratio")
    assert "customer.full_name" in {d.ref for d in strict.unavailable_dimensions}


def test_no_request_field_changes_the_policy(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    for extra in ({"governance": {"mode": "off"}}, {"mode": "off"}, {"min_group_size": 0}):
        error = refusal(
            lambda extra=extra: svc.compile_query(users.caller("admin"), {**REGION, **extra})
        )
        assert error.code == ServiceErrorCode.INVALID_INPUT


def test_a_relaxed_policy_adds_its_warning_to_every_compile(
    fresh_service_world: tuple[Engine, Users],
) -> None:
    engine, users = fresh_service_world
    relaxed = sup.services(engine, GovernancePolicy.custom(allow_personal_filters=True))
    response = relaxed.compile_query(users.caller("viewer"), REGION)
    assert any(w.startswith("Governance mode custom relaxes") for w in response.warnings)
    strict = sup.services(engine).compile_query(users.caller("viewer"), REGION)
    assert not any("Governance mode" in w for w in strict.warnings)
