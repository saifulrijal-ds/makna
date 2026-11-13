"""WP-12 criterion 1 through the services: what each column of the section 11 matrix can read
and run. The matrix cells themselves are checked cell by cell in the unit tier
(`tests/unit/test_service_permissions.py`); here each column calls the four services."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from makna.services import Caller, ServiceError
from makna.services import permissions as p
from makna.services.errors import ServiceErrorCode

from . import service_support as sup
from .service_support import Users

COLUMNS = ("viewer", "curator", "approver", "admin", "agent token")


def caller_of(users: Users, column: str) -> Caller:
    if column == "agent token":
        return users.token("admin")
    return users.caller(column)


@pytest.mark.parametrize("column", COLUMNS)
def test_each_column_reads_and_compiles_as_the_matrix_says(
    service_world: tuple[Engine, Users], column: str
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = caller_of(users, column)
    assert p.can_read_approved(caller) and p.can_compile(caller)  # "yes" in every column
    assert svc.search(caller, "kredit macet").hits
    assert svc.get_definition(caller, "metric:npf_ratio").id == "metric:npf_ratio"
    assert svc.list_related(caller, "metric:npf_ratio").related
    assert svc.compile_query(caller, {"metric": "npf_ratio"}).sql


@pytest.mark.parametrize("column", COLUMNS)
def test_drafts_follow_the_see_drafts_row(service_world: tuple[Engine, Users], column: str) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    caller = caller_of(users, column)
    sees = p.can_see_drafts(caller)
    assert sees is (p.MATRIX[p.Permission.SEE_DRAFTS][p.column_of(caller)] is p.Grant.YES)
    searched = [h.id for h in svc.search(caller, "hapus buku write-off", limit=10).hits]
    assert ("metric:write_off_amount" in searched) is sees
    if sees:
        assert svc.get_definition(caller, "metric:write_off_amount").status == "draft"
        assert "metric:write_off_amount" in {
            i.id for i in svc.list_related(caller, "entity:contract_snapshot").related
        }
    else:
        with pytest.raises(ServiceError) as raised:
            svc.get_definition(caller, "metric:write_off_amount")
        assert raised.value.code == ServiceErrorCode.NOT_FOUND
        assert "metric:write_off_amount" not in {
            i.id for i in svc.list_related(caller, "entity:contract_snapshot").related
        }


def test_a_token_whose_user_is_an_admin_still_cannot_see_a_draft(
    service_world: tuple[Engine, Users],
) -> None:
    engine, users = service_world
    admin = users.caller("admin")
    token = users.token("admin")
    assert token.role == "admin" and token.effective_role == "viewer"
    svc = sup.services(engine)
    assert svc.get_definition(admin, "metric:write_off_amount")
    with pytest.raises(ServiceError):
        svc.get_definition(token, "metric:write_off_amount")


def test_the_test_bench_is_not_a_service_and_is_denied_to_a_token(
    service_world: tuple[Engine, Users],
) -> None:
    _, users = service_world
    assert p.can_use_test_bench(users.caller("viewer"))
    assert not p.can_use_test_bench(users.token("viewer"))
