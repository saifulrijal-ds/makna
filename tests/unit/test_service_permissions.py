"""WP-12 criterion 1 (the pure part): every cell of the permission matrix of ARCHITECTURE
section 11, written out here from the table, for the read and run rows and the write rows, with
the agent-token column included. The services that use it are tested in the integration tier.
"""

from __future__ import annotations

import pytest

from makna.services import Caller
from makna.services import permissions as p
from makna.services.errors import PermissionDenied, ServiceError, ServiceErrorCode

COLUMNS = ("viewer", "curator", "approver", "admin", "agent token")

# The table of section 11, row by row: viewer, curator, approver, admin, agent token.
TABLE = {
    "Search and read approved definitions": ("yes", "yes", "yes", "yes", "yes"),
    "See drafts and proposals": ("no", "yes", "yes", "yes", "no"),
    "Compile a query": ("yes", "yes", "yes", "yes", "yes"),
    "Use the test bench": ("yes", "yes", "yes", "yes", "n/a"),
    "Create, edit, submit, withdraw proposals": ("no", "yes", "yes", "yes", "no"),
    "Approve, reject, deprecate": ("no", "owner team only", "yes", "yes", "no"),
    "See audit log": ("no", "yes", "yes", "yes", "no"),
    "See tool-call log": ("no", "no", "yes", "yes", "no"),
    "Manage users, tokens of others, catalog sync, index": ("no", "no", "no", "yes", "no"),
}


def caller_of(column: str) -> Caller:
    if column == "agent token":
        # The token's user is an admin: a token never gets more than a viewer.
        return Caller(user_id=1, role="admin", channel="mcp", token_id=7)
    return Caller(user_id=1, role=column, channel="web")


def test_the_table_here_covers_every_permission_of_the_module() -> None:
    assert {permission.value for permission in p.Permission} == set(TABLE)


@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("row", sorted(TABLE))
def test_every_cell_of_the_matrix(row: str, column: str) -> None:
    permission = p.Permission(row)
    expected = TABLE[row][COLUMNS.index(column)]
    assert p.grant(caller_of(column), permission).value == expected


FUNCTIONS = {
    "Search and read approved definitions": p.can_read_approved,
    "See drafts and proposals": p.can_see_drafts,
    "Compile a query": p.can_compile,
    "Use the test bench": p.can_use_test_bench,
    "Create, edit, submit, withdraw proposals": p.can_edit_proposals,
    "See audit log": p.can_see_audit_log,
    "See tool-call log": p.can_see_tool_call_log,
    "Manage users, tokens of others, catalog sync, index": p.can_manage,
}


@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("row", sorted(FUNCTIONS))
def test_one_function_per_permission_agrees_with_the_cell(row: str, column: str) -> None:
    """Only a plain "yes" allows. "n/a" (the test bench for a token) is denied."""
    expected = TABLE[row][COLUMNS.index(column)] == "yes"
    assert FUNCTIONS[row](caller_of(column)) is expected


@pytest.mark.parametrize("column", COLUMNS)
def test_the_approval_cell_is_not_a_blanket_yes_for_a_curator(column: str) -> None:
    caller = caller_of(column)
    expected = TABLE["Approve, reject, deprecate"][COLUMNS.index(column)]
    assert p.approval_grant(caller).value == expected
    assert p.allows(caller, p.Permission.DECIDE_PROPOSALS) is (expected == "yes")


@pytest.mark.parametrize("role", ["viewer", "curator", "approver", "admin"])
def test_a_token_uses_the_agent_column_whatever_its_users_role(role: str) -> None:
    caller = Caller(user_id=3, role=role, channel="mcp", token_id=9)
    assert p.column_of(caller) == "agent_token"
    assert caller.effective_role == "viewer"
    assert p.can_read_approved(caller) and p.can_compile(caller)
    assert not p.can_see_drafts(caller)
    assert not p.can_use_test_bench(caller)
    assert not p.can_edit_proposals(caller)
    assert not p.can_manage(caller)


def test_require_raises_a_structured_error_that_is_also_a_permission_error() -> None:
    caller = caller_of("viewer")
    with pytest.raises(PermissionDenied) as raised:
        p.require(caller, p.Permission.SEE_DRAFTS)
    error = raised.value
    assert isinstance(error, ServiceError) and isinstance(error, PermissionError)
    assert error.code == ServiceErrorCode.PERMISSION_DENIED
    assert error.to_dict()["error"]["code"] == "PERMISSION_DENIED"
    assert error.details == {"permission": "see_drafts"}
    p.require(caller, p.Permission.COMPILE)  # allowed: no error


def test_the_test_bench_is_denied_to_a_token() -> None:
    with pytest.raises(PermissionDenied):
        p.require(caller_of("agent token"), p.Permission.USE_TEST_BENCH)


# --- the Caller itself ---


@pytest.mark.parametrize("role", ["", "root", "Viewer", "agent", "owner", "ADMIN"])
def test_an_unknown_role_cannot_be_built(role: str) -> None:
    with pytest.raises(ValueError, match="unknown role"):
        Caller(user_id=1, role=role, channel="web")


@pytest.mark.parametrize("channel", ["", "http", "MCP", "api", "stdio"])
def test_an_unknown_channel_cannot_be_built(channel: str) -> None:
    with pytest.raises(ValueError, match="unknown channel"):
        Caller(user_id=1, role="viewer", channel=channel)


def test_a_caller_needs_a_user_or_a_token() -> None:
    with pytest.raises(ValueError, match="user_id or a token_id"):
        Caller(user_id=None, role="viewer", channel="cli")
    assert Caller(user_id=None, role="viewer", channel="mcp", token_id=4).is_token


@pytest.mark.parametrize("bad", ["1", 1.5, True])
def test_ids_must_be_whole_numbers(bad: object) -> None:
    with pytest.raises(ValueError, match="whole number"):
        Caller(user_id=bad, role="viewer", channel="cli")  # type: ignore[arg-type]


def test_teams_are_a_tuple_of_names_and_the_caller_is_frozen() -> None:
    caller = Caller(user_id=1, role="curator", channel="web", teams=["credit-risk", "risk"])  # type: ignore[arg-type]
    assert caller.teams == ("credit-risk", "risk")
    with pytest.raises(ValueError, match="teams"):
        Caller(user_id=1, role="curator", channel="web", teams="credit-risk")  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        caller.role = "admin"  # type: ignore[misc]
