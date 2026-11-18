"""Who may do what to a given object (ARCHITECTURE section 11), as pure functions.

`permissions.py` holds the table of roles. This module holds the rules the table cannot: the
owner team of an object, the two-person rule, a change of owner, and the governance team that
decides what loosens sensitivity (D-20, R-6, R-18). Nothing here reads a database or a file; the
caller gives the models and the authors, so every rule can be tested with a table.

`may_decide` is the one function for approve, reject, and deprecate. It returns a `Decision` with
a reason code for every refusal. `may_edit` is the one for the proposal actions.

Notes on what is and is not compared:

- **Sensitivity is compared on the parsed models**, never on text: an object's level, each
  dimension's effective level (its own, else the entity's), and each dimension's masking rule. A
  dimension that is new in the proposal counts as loosened when it is less strict than the level
  the entity has in the official version, so renaming a personal dimension cannot avoid the rule.
  Levels the catalog derives for columns are not part of a definition and are not compared.
  A rule (`Rule`) carries no sensitivity of its own in the file format, so there is nothing to
  compare for rules.
- **Masking.** A masking rule that is removed, or replaced by a finer one, is a loosening. From
  strictest to finest: `redact`, then `truncate_date` (year, quarter, month), then `bucket` (a
  wider bucket is stricter). A change between `bucket` and `truncate_date` is a loosening.
- **Reject and withdraw** need the role (or the owner team of a curator) and nothing else: the
  two-person rule does not apply to them, because refusing a change never makes anything official.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from enum import StrEnum

from makna.model.definitions import (
    AnyDefinition,
    Dimension,
    Entity,
    Masking,
    MaskRedact,
    MaskTruncateDate,
)
from makna.model.sensitivity import rank
from makna.services import permissions
from makna.services.caller import Caller
from makna.services.permissions import Grant

APPROVE = "approve"
REJECT = "reject"
DEPRECATE = "deprecate"
DECISIONS = (APPROVE, REJECT, DEPRECATE)


class Refusal(StrEnum):
    TOKEN = "token_cannot_write"
    ROLE = "role_cannot_decide"
    NOT_OWNER_TEAM = "not_in_owner_team"
    SELF_APPROVAL = "author_cannot_approve"
    OWNER_CHANGE = "owner_change_needs_approver"
    GOVERNANCE_TEAM = "governance_team_needed"
    EDIT_ROLE = "role_cannot_edit"
    NEW_OBJECT_TEAM = "new_object_team_not_yours"
    NOT_AUTHOR = "not_the_author"
    NO_ACCOUNT = "no_active_account"


@dataclass(frozen=True)
class Decision:
    """The answer of a rule: allowed, or one reason code and a message that says what to do."""

    allowed: bool
    refusal: Refusal | None = None
    message: str = ""
    needs: tuple[str, ...] = ()
    """For a governance refusal, what in the proposal needs the governance team."""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class ApprovalSettings:
    governance_team: str
    allow_self_approval: bool = False


_ALLOWED = Decision(True)


def _refuse(refusal: Refusal, message: str, needs: Iterable[str] = ()) -> Decision:
    return Decision(False, refusal, message, tuple(needs))


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _in_team(caller: Caller, team: str) -> bool:
    return any(_same(t, team) for t in caller.teams)


def owner_team(official: AnyDefinition | None, proposal: AnyDefinition | None) -> str | None:
    """The owner team approval rights come from: the official file's, and for a new object the
    proposal's (R-6). A proposal that names another team does not move the rights."""
    found = official if official is not None else proposal
    return found.owner.team if found is not None else None


# --- loosening (D-20) ----------------------------------------------------------------------


def _mask_strength(mask: Masking) -> tuple[int, float]:
    if isinstance(mask, MaskRedact):
        return (3, 0.0)
    if isinstance(mask, MaskTruncateDate):
        return (2, {"year": 3.0, "quarter": 2.0, "month": 1.0}[mask.grain])
    return (1, float(mask.width))  # bucket: a wider bucket hides more


def _mask_loosened(old: Masking | None, new: Masking | None) -> bool:
    if old is None:
        return False
    if new is None:
        return True
    old_kind, old_value = _mask_strength(old)
    new_kind, new_value = _mask_strength(new)
    if old_kind == new_kind:
        return new_value < old_value
    return new_kind < old_kind or old_kind != 3 and new_kind != 3


def _level(entity: Entity, dimension: Dimension) -> str:
    return dimension.sensitivity or entity.sensitivity


def loosenings(official: AnyDefinition | None, proposal: AnyDefinition) -> list[str]:
    """What in `proposal` the governance team must decide (D-20), as short sentences without any
    value from the definition: a less strict level, a removed or finer masking rule, a new object
    with a personal dimension. Empty when none."""
    found: list[str] = []
    if official is None:
        if isinstance(proposal, Entity) and any(
            _level(proposal, d) == "personal" for d in proposal.dimensions
        ):
            found.append("a new object with a personal dimension")
        return found
    if rank(proposal.sensitivity) < rank(official.sensitivity):
        found.append("a lower sensitivity for the object")
    if isinstance(official, Entity) and isinstance(proposal, Entity):
        before = {d.name: d for d in official.dimensions}
        for dimension in proposal.dimensions:
            old = before.get(dimension.name)
            if old is None:
                # New (or renamed): compared with what the entity was, so a rename cannot hide it.
                if rank(_level(proposal, dimension)) < rank(official.sensitivity):
                    found.append(
                        f"a new dimension {dimension.name} at a lower level than the entity"
                    )
                continue
            if rank(_level(proposal, dimension)) < rank(_level(official, old)):
                found.append(f"a lower sensitivity for dimension {dimension.name}")
            if _mask_loosened(old.masking, dimension.masking):
                found.append(f"a removed or finer masking rule for dimension {dimension.name}")
    return found


# --- decisions -----------------------------------------------------------------------------


def may_decide(
    caller: Caller,
    caller_email: str | None,
    official: AnyDefinition | None,
    proposal: AnyDefinition | None,
    authors: Collection[str],
    settings: ApprovalSettings,
    action: str = APPROVE,
) -> Decision:
    """May `caller` approve, reject, or deprecate this object?

    `official` and `proposal` are the models as they stand now (read after the lock was taken);
    `authors` are the emails of everyone who authored a commit on the proposal since the object
    was last approved, external authors included. `caller_email` is the signed-in user's email.
    """
    if action not in DECISIONS:
        raise ValueError(f"unknown action {action!r}")
    if caller.is_token:
        return _refuse(Refusal.TOKEN, "An API token cannot change definitions.")
    grant = permissions.approval_grant(caller)
    if grant is Grant.NO or grant is Grant.NOT_APPLICABLE:
        return _refuse(
            Refusal.ROLE, "Your role does not allow approving, rejecting, or deprecating."
        )
    team = owner_team(official, proposal)
    if grant is Grant.OWNER_TEAM and (team is None or not _in_team(caller, team)):
        return _refuse(
            Refusal.NOT_OWNER_TEAM,
            "A curator may decide only for an object that the curator's team owns.",
        )
    if action != APPROVE:
        return _ALLOWED  # refusing or retiring needs the role and the owner team, nothing more
    if proposal is None:
        raise ValueError("approve needs a proposal")
    is_approver = grant is Grant.YES
    if not settings.allow_self_approval and caller_email is not None:
        if any(_same(caller_email, a) for a in authors):
            return _refuse(
                Refusal.SELF_APPROVAL,
                "You wrote part of this change, so someone else must approve it.",
            )
    if not settings.allow_self_approval and caller_email is None:
        return _refuse(Refusal.NO_ACCOUNT, "Only a signed-in user with an email can approve.")
    if official is not None and not _same(official.owner.team, proposal.owner.team):
        if not is_approver:
            return _refuse(
                Refusal.OWNER_CHANGE,
                "Changing the owner team needs an approver or an administrator.",
            )
    needs = loosenings(official, proposal)
    if needs and not _in_team(caller, settings.governance_team):
        return _refuse(
            Refusal.GOVERNANCE_TEAM,
            "This change affects sensitivity, so a member of the governance team must approve it.",
            needs,
        )
    return _ALLOWED


def may_create_new(caller: Caller, proposal: AnyDefinition) -> Decision:
    """A curator may create a new object only for a team the curator is in; an approver or an
    administrator for any team."""
    if caller.is_token:
        return _refuse(Refusal.TOKEN, "An API token cannot change definitions.")
    if not permissions.can_edit_proposals(caller):
        return _refuse(Refusal.EDIT_ROLE, "Your role does not allow writing proposals.")
    if caller.effective_role in ("approver", "admin"):
        return _ALLOWED
    if not _in_team(caller, proposal.owner.team):
        return _refuse(
            Refusal.NEW_OBJECT_TEAM,
            "A new object must be owned by one of your teams.",
        )
    return _ALLOWED


def may_edit(caller: Caller, caller_email: str | None, authors: Collection[str] | None) -> Decision:
    """May `caller` edit, submit, or withdraw an existing proposal (or create one for an
    existing object, with `authors` None)? Any curator or above may start a change to an existing
    object; only an author of the proposal, or an approver or an administrator, may go on with
    it."""
    if caller.is_token:
        return _refuse(Refusal.TOKEN, "An API token cannot change definitions.")
    if not permissions.can_edit_proposals(caller):
        return _refuse(Refusal.EDIT_ROLE, "Your role does not allow writing proposals.")
    if authors is None or caller.effective_role in ("approver", "admin"):
        return _ALLOWED
    if caller_email is None or not any(_same(caller_email, a) for a in authors):
        return _refuse(Refusal.NOT_AUTHOR, "Only the author of this proposal can change it.")
    return _ALLOWED
