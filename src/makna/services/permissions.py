"""The permission matrix of ARCHITECTURE section 11, as data, with one function per permission.

`MATRIX` is the table of section 11: a row per permission, a column per role and one for the
agent token. A caller that holds a token always uses the agent token column, whatever its user's
role (it acts with at most viewer permissions, and never writes). Everything not granted is
denied; an unknown role or channel cannot be built (`Caller`).

Read and run permissions (search, read, drafts, compile, test bench) are checked by the services
of WP-12. The write permissions (proposals, approval, audit log, tool-call log, administration)
are in the matrix and have their functions here so that one module holds the whole table, but
their use belongs to later packages. The rules below the table, **who may approve a given
object** (owner team, the two-person rule, changing the owner, loosening sensitivity under D-20),
are WP-18's: `approval_grant` returns only the cell of the table, "owner team only" for a
curator, and decides nothing about a particular object.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from makna.services.caller import ROLES, Caller
from makna.services.errors import PermissionDenied

AGENT_TOKEN = "agent_token"
COLUMNS: tuple[str, ...] = (*ROLES, AGENT_TOKEN)


class Grant(StrEnum):
    YES = "yes"
    NO = "no"
    OWNER_TEAM = "owner team only"
    NOT_APPLICABLE = "n/a"


class Permission(StrEnum):
    READ_APPROVED = "Search and read approved definitions"
    SEE_DRAFTS = "See drafts and proposals"
    COMPILE = "Compile a query"
    USE_TEST_BENCH = "Use the test bench"
    EDIT_PROPOSALS = "Create, edit, submit, withdraw proposals"
    DECIDE_PROPOSALS = "Approve, reject, deprecate"
    SEE_AUDIT_LOG = "See audit log"
    SEE_TOOL_CALL_LOG = "See tool-call log"
    MANAGE = "Manage users, tokens of others, catalog sync, index"


def _row(
    viewer: Grant, curator: Grant, approver: Grant, admin: Grant, token: Grant
) -> dict[str, Grant]:
    return dict(zip(COLUMNS, (viewer, curator, approver, admin, token), strict=True))


_Y, _N, _OT, _NA = Grant.YES, Grant.NO, Grant.OWNER_TEAM, Grant.NOT_APPLICABLE

MATRIX: Mapping[Permission, Mapping[str, Grant]] = {
    Permission.READ_APPROVED: _row(_Y, _Y, _Y, _Y, _Y),
    Permission.SEE_DRAFTS: _row(_N, _Y, _Y, _Y, _N),
    Permission.COMPILE: _row(_Y, _Y, _Y, _Y, _Y),
    Permission.USE_TEST_BENCH: _row(_Y, _Y, _Y, _Y, _NA),
    Permission.EDIT_PROPOSALS: _row(_N, _Y, _Y, _Y, _N),
    Permission.DECIDE_PROPOSALS: _row(_N, _OT, _Y, _Y, _N),
    Permission.SEE_AUDIT_LOG: _row(_N, _Y, _Y, _Y, _N),
    Permission.SEE_TOOL_CALL_LOG: _row(_N, _N, _Y, _Y, _N),
    Permission.MANAGE: _row(_N, _N, _N, _Y, _N),
}


def column_of(caller: Caller) -> str:
    """The column of the matrix that applies to `caller`."""
    return AGENT_TOKEN if caller.is_token else caller.role


def grant(caller: Caller, permission: Permission) -> Grant:
    """The cell of the matrix for this caller and permission."""
    return MATRIX[permission][column_of(caller)]


def allows(caller: Caller, permission: Permission) -> bool:
    """True only for a plain "yes". "n/a" and "owner team only" are not a blanket yes."""
    return grant(caller, permission) is Grant.YES


def require(caller: Caller, permission: Permission) -> None:
    """Raise `PermissionDenied` unless `caller` has `permission` outright."""
    if not allows(caller, permission):
        raise PermissionDenied(
            f"Your role does not allow this: {permission.value.lower()}.",
            {"permission": permission.name.lower()},
        )


def can_read_approved(caller: Caller) -> bool:
    return allows(caller, Permission.READ_APPROVED)


def can_see_drafts(caller: Caller) -> bool:
    return allows(caller, Permission.SEE_DRAFTS)


def can_compile(caller: Caller) -> bool:
    return allows(caller, Permission.COMPILE)


def can_use_test_bench(caller: Caller) -> bool:
    return allows(caller, Permission.USE_TEST_BENCH)


def can_edit_proposals(caller: Caller) -> bool:
    return allows(caller, Permission.EDIT_PROPOSALS)


def approval_grant(caller: Caller) -> Grant:
    """The approval cell: yes, no, or "owner team only" (WP-18 applies the rules for an object)."""
    return grant(caller, Permission.DECIDE_PROPOSALS)


def can_see_audit_log(caller: Caller) -> bool:
    return allows(caller, Permission.SEE_AUDIT_LOG)


def can_see_tool_call_log(caller: Caller) -> bool:
    return allows(caller, Permission.SEE_TOOL_CALL_LOG)


def can_manage(caller: Caller) -> bool:
    return allows(caller, Permission.MANAGE)
