"""WP-12 criterion 2 (search never returns a draft to a viewer or a token), the guidance when
`kinds` leaves out metrics, and the bounds on the input of `search`."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine

from makna.services import Caller, ServiceError
from makna.services.errors import ServiceErrorCode

from . import service_support as sup
from .service_support import Users

DRAFT_QUERY = "hapus buku write-off"  # finds metric:write_off_amount, a draft in the sample


def error_of(call) -> ServiceError:  # type: ignore[no-untyped-def]
    with pytest.raises(ServiceError) as raised:
        call()
    return raised.value


def test_a_viewer_and_a_token_never_get_a_draft_and_a_curator_can(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    for caller in (users.caller("viewer"), users.token("admin"), users.token("curator")):
        found = svc.search(caller, DRAFT_QUERY, limit=10)
        assert "metric:write_off_amount" not in [hit.id for hit in found.hits], caller
        assert all(hit.status is None for hit in found.hits)  # only approved hits
    for role in ("curator", "approver", "admin"):
        found = svc.search(users.caller(role), DRAFT_QUERY, limit=10)
        draft = next(hit for hit in found.hits if hit.id == "metric:write_off_amount")
        assert draft.status == "draft"


def test_a_deprecated_metric_appears_only_when_asked_for(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    for caller in (users.caller("viewer"), users.token()):
        plain = svc.search(caller, "NPL ratio retired", limit=10)
        assert "metric:npl_ratio" not in [h.id for h in plain.hits]
        asked = svc.search(caller, "NPL ratio retired", limit=10, include_deprecated=True)
        hit = next(h for h in asked.hits if h.id == "metric:npl_ratio")
        assert hit.status == "deprecated"


def test_a_personal_column_is_hidden_from_viewers_and_tokens(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    for caller in (users.caller("viewer"), users.token()):
        found = svc.search(caller, "national id", kinds=["column"], limit=10)
        assert not [h for h in found.hits if h.id.endswith("customers.national_id")]
    found = svc.search(users.caller("curator"), "national id", kinds=["column"], limit=10)
    assert any(h.id.endswith("customers.national_id") for h in found.hits)


def test_a_hit_has_the_fields_of_section_12(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    found = sup.services(engine).search(users.caller("viewer"), "kredit macet")
    assert found.confidence == "high" and found.guidance is None
    top = found.hits[0]
    assert (top.id, top.label, top.kind, top.type, top.unit) == (
        "metric:npf_ratio",
        "NPF Ratio",
        "metric",
        "ratio",
        "percent",
    )
    assert "kredit" in top.matched_terms
    assert all(len(hit.description) <= 200 for hit in found.hits)
    shown = found.to_dict()
    assert "status" not in shown["hits"][0]  # approved: nothing to say


def test_the_labels_of_dimension_and_rule_hits_come_from_the_definition(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    found = sup.services(engine).search(
        users.caller("viewer"), "non-performing", kinds=["dimension", "metric"], limit=10
    )
    assert found.hits
    for hit in found.hits:
        assert hit.label and hit.kind in ("metric", "dimension")


# --- guidance ---


def test_low_confidence_guidance_names_the_kinds_when_metrics_are_left_out(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    nothing = "website conversion funnel"
    default = svc.search(caller, nothing)
    assert default.confidence == "low"
    assert default.guidance is not None and "no approved metric exists" in default.guidance
    with_metrics = svc.search(caller, nothing, kinds=["metric", "entity"])
    assert with_metrics.guidance == default.guidance
    narrowed = svc.search(caller, nothing, kinds=["entity", "dimension"])
    assert narrowed.confidence == "low"
    assert narrowed.guidance is not None
    assert "no approved metric" not in narrowed.guidance
    assert "entity, dimension" in narrowed.guidance
    assert "do not approximate" in narrowed.guidance
    only_terms = svc.search(caller, nothing, kinds=["glossary_term"])
    assert "glossary_term" in (only_terms.guidance or "")


# --- input bounds ---

BAD_INPUT = [
    ({"query": ""}, "query"),
    ({"query": "   \n"}, "query"),
    ({"query": "x" * 501}, "query"),
    ({"query": 5}, "query"),
    ({"query": "npf", "limit": 0}, "limit"),
    ({"query": "npf", "limit": 11}, "limit"),
    ({"query": "npf", "limit": True}, "limit"),
    ({"query": "npf", "limit": "5"}, "limit"),
    ({"query": "npf", "kinds": ["rule"]}, "kinds"),
    ({"query": "npf", "kinds": ["metric", "bogus"]}, "kinds"),
    ({"query": "npf", "kinds": []}, "kinds"),
    ({"query": "npf", "kinds": "metric"}, "kinds"),
    ({"query": "npf", "domain": "Lending; DROP"}, "domain"),
    ({"query": "npf", "domain": 7}, "domain"),
    ({"query": "npf", "include_deprecated": "yes"}, "include_deprecated"),
]


@pytest.mark.parametrize(("arguments", "field"), BAD_INPUT)
def test_bad_input_is_invalid_input_naming_the_field_before_any_search(
    service_world: tuple[Engine, Users],
    arguments: dict[str, Any],
    field: str,
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    before = sup.log_count(engine)
    error = error_of(lambda: svc.search(users.caller("viewer"), **arguments))
    assert error.code == ServiceErrorCode.INVALID_INPUT
    assert error.details["field"] == field
    assert field in error.message
    row = sup.last_log(engine)
    assert sup.log_count(engine) == before + 1
    assert (row["tool"], row["status"], row["error_code"]) == ("search", "refused", "INVALID_INPUT")


def test_the_limits_themselves_are_allowed(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = users.caller("viewer")
    assert len(svc.search(caller, "x" * 500, limit=10).hits) <= 10
    assert len(svc.search(caller, "outstanding", limit=1).hits) == 1
    assert len(svc.search(caller, "outstanding", limit=10).hits) <= 10
    assert svc.search(caller, "outstanding", kinds=list(("metric", "entity")), domain="lending")


def test_a_missing_index_is_an_internal_error_with_no_text_of_the_cause(
    migrated_engine: Engine,
) -> None:
    users = sup.add_users(migrated_engine)
    svc = sup.services(migrated_engine)
    error = error_of(lambda: svc.search(users.caller("viewer"), "npf"))
    assert error.code == ServiceErrorCode.INTERNAL
    assert "index build" in error.message and error.details == {}
    assert sup.last_log(migrated_engine)["status"] == "error"


def test_the_caller_type_is_used_as_given() -> None:
    assert Caller(user_id=1, role="viewer", channel="cli").effective_role == "viewer"
