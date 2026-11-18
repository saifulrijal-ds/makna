"""WP-20: the `resolve` service, and `ambiguous_between` in the search service (ARCHITECTURE 8.4,
12, 14). The index is built with the hash provider, so these tests check behaviour, not the numbers
the evals measure on the real model."""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from sqlalchemy import Engine, select

from makna.index.tables import tool_call_log
from makna.model.request import QueryRequest
from makna.search.confidence import AMBIGUOUS_GUIDANCE
from makna.services import ServiceError
from makna.services.errors import ServiceErrorCode

from . import service_support as sup
from .service_support import Users

TODAY = datetime.date(2026, 7, 1)
AMBIGUOUS = "Berapa NPF kita?"
CLEAR = "What is the average ticket size by region last month?"


def refused(call: Any) -> ServiceError:
    with pytest.raises(ServiceError) as raised:
        call()
    return raised.value


# --- ambiguous_between in search --------------------------------------------------------------


def test_search_names_two_metrics_the_question_does_not_tell_apart(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    found = sup.services(engine).search(users.caller("viewer"), "NPF")
    assert found.ambiguous_between == ["metric:npf_ratio", "metric:npf_outstanding_principal"] or (
        len(found.ambiguous_between) == 2
        and all(i.startswith("metric:npf") for i in found.ambiguous_between)
    )
    ids = [hit.id for hit in found.hits]
    assert all(i in ids for i in found.ambiguous_between)
    assert found.guidance == AMBIGUOUS_GUIDANCE
    assert found.to_dict()["ambiguous_between"] == found.ambiguous_between
    assert list(found.to_dict())[:2] == ["confidence", "ambiguous_between"]


def test_search_has_no_ambiguous_between_when_one_metric_is_clearly_better(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    found = sup.services(engine).search(users.caller("viewer"), "average ticket size")
    assert found.hits[0].id == "metric:average_ticket_size"
    assert found.ambiguous_between == []
    assert "ambiguous_between" not in found.to_dict()


def test_search_with_low_confidence_names_no_ambiguity(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    found = sup.services(engine).search(users.caller("viewer"), "employee attrition by region")
    assert found.confidence == "low"
    assert found.ambiguous_between == []


def test_search_with_a_limit_of_one_still_reports_the_ambiguity(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    many = svc.search(users.caller("viewer"), "NPF", limit=10)
    one = svc.search(users.caller("viewer"), "NPF", limit=1)
    assert len(one.hits) == 1
    assert one.ambiguous_between == many.ambiguous_between != []


def test_search_for_other_kinds_has_no_ambiguous_between(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    found = sup.services(engine).search(users.caller("viewer"), "NPF", kinds=["glossary_term"])
    assert found.ambiguous_between == []


# --- resolve --------------------------------------------------------------------------------


def test_resolve_builds_a_request_and_logs_the_question(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    result = svc.resolve(users.caller("viewer", "web"), CLEAR, TODAY)
    assert result.status == "resolved"
    assert result.request == QueryRequest(
        metric="average_ticket_size",
        dimensions=("branch.region_name",),
        time_start="2026-06-01",
        time_end="2026-07-01",
    )
    assert result.today == TODAY
    row = sup.last_log(engine)
    assert row["tool"] == "resolve" and row["status"] == "ok" and row["channel"] == "web"
    assert row["request"] == {"question": CLEAR, "today": "2026-07-01"}
    assert row["result"]["status"] == "resolved"
    assert row["result"]["ids"][0] == "metric:average_ticket_size"


def test_resolve_does_not_choose_between_two_metrics(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    result = sup.services(engine).resolve(users.caller("viewer", "web"), "NPF", TODAY)
    assert result.status == "ambiguous"
    assert result.request is None
    assert len(result.ambiguous_between) == 2


def test_resolve_never_offers_a_draft_whoever_asks(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    for role in ("viewer", "curator", "approver", "admin"):
        result = svc.resolve(users.caller(role, "web"), "hapus buku write-off", TODAY)
        assert "metric:write_off_amount" not in [h.id for h in result.hits], role
        assert "metric:write_off_amount" not in [c.id for c in result.candidates], role
        assert result.request is None or result.request.metric != "write_off_amount"


def test_resolve_is_refused_for_a_token_even_an_admins(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    for role in ("admin", "viewer"):
        error = refused(lambda role=role: svc.resolve(users.token(role), CLEAR, TODAY))
        assert error.code == ServiceErrorCode.PERMISSION_DENIED
        row = sup.last_log(engine)
        assert row["tool"] == "resolve" and row["status"] == "refused"
        assert row["token_id"] == users.token_id


@pytest.mark.parametrize("question", ["", "   ", "x" * 501, None, 7])
def test_resolve_checks_the_question(service_world: tuple[Engine, Users], question: object) -> None:
    engine, users = service_world
    error = refused(
        lambda: sup.services(engine).resolve(users.caller("viewer", "web"), question, TODAY)  # type: ignore[arg-type]
    )
    assert error.code == ServiceErrorCode.INVALID_INPUT
    assert error.details["field"] == "question"


def test_resolve_checks_the_date(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    error = refused(
        lambda: sup.services(engine).resolve(users.caller("viewer", "web"), CLEAR, "2026-07-01")  # type: ignore[arg-type]
    )
    assert error.code == ServiceErrorCode.INVALID_INPUT and error.details["field"] == "today"


def test_resolve_redacts_the_question_in_the_log(service_world: tuple[Engine, Users]) -> None:
    engine, users = service_world
    sup.services(engine).resolve(
        users.caller("viewer", "web"), "disbursement for jane.doe@example.com 081234567890", TODAY
    )
    request = sup.last_log(engine)["request"]
    assert "jane.doe@example.com" not in request["question"]
    assert "081234567890" not in request["question"]
    assert "[REDACTED]" in request["question"]


def test_resolve_writes_exactly_one_row_and_runs_no_compile(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    before = sup.log_count(engine)
    sup.services(engine).resolve(users.caller("curator", "web"), CLEAR, TODAY)
    assert sup.log_count(engine) == before + 1
    with engine.connect() as connection:
        tools = connection.execute(
            select(tool_call_log.c.tool).order_by(tool_call_log.c.id.desc()).limit(1)
        ).scalars()
        assert list(tools) == ["resolve"]


# --- D-38: the ratio is on the pre-fusion score -------------------------------------------------


def test_a_hit_carries_the_weighted_pre_fusion_score(service_world: tuple[Engine, Users]) -> None:
    from makna.model.search import SearchSettings
    from makna.search.embedding import HashProvider
    from makna.search.hybrid import HybridSearch

    engine, _ = service_world
    settings = SearchSettings()
    found = HybridSearch(engine, HashProvider(), settings).search(
        "average ticket size", role="viewer", limit=10
    )
    assert found.hits
    for h in found.hits:
        expected = settings.w_keyword * (h.keyword_score or 0.0) + settings.w_vector * (
            h.vector_similarity or 0.0
        )
        assert h.pre_fusion_score == pytest.approx(expected)
    assert any(h.pre_fusion_score > 0 for h in found.hits)


def test_search_and_resolve_ask_the_same_question_of_the_same_pair(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    for question in ("NPF", "average ticket size", "disbursement", "outstanding principal"):
        searched = svc.search(users.caller("viewer"), question, kinds=["metric"], limit=10)
        resolved = svc.resolve(users.caller("viewer", "web"), question, TODAY)
        if resolved.status == "ambiguous":
            assert list(resolved.ambiguous_between) == searched.ambiguous_between, question
        else:
            assert not searched.ambiguous_between or resolved.status == "no_match", question
