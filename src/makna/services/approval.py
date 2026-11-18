"""Who may do what to a given object (ARCHITECTURE section 11), as pure functions.

`permissions.py` holds the table of roles. This module holds the rules the table cannot: the
owner team of an object, the two-person rule, a change of owner, and the governance team that
decides what loosens sensitivity (D-20, R-6, R-18). Nothing here reads a database or a file; the
caller gives the models and the authors, so every rule can be tested with a table.

`may_decide` is the one function for approve, reject, and deprecate. It returns a `Decision` with
a reason code for every refusal. `may_edit` is the one for the proposal actions.

Notes on what is and is not compared:

- **Sensitivity is compared on the parsed models**, never on text: an object's level, each
  dimension's effective level (its own, else the entity's), and each dimension's masking rule.
  Dimensions are matched by name; one that is gone is matched with a new one that reads the same
  parsed columns (a rename); and **every new or changed dimension is compared with the strictest
  official dimension that reads the same parsed columns**, so a sibling (`birth_month` next to a
  masked `birth_year`), a swap of names, and a dimension re-pointed at a protected column all need
  the governance team when they show that column at a lower level or with less masking. A new
  dimension on a column no official dimension reads is compared with the entity's official level.
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
from makna.model.expressions import ExpressionError, dimension_columns
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


def _expr_key(dimension: Dimension) -> str:
    """What a dimension reads: its expression, or its own name when it has none (the column)."""
    return (dimension.expr or dimension.name).strip().lower()


def _weaker(entity_before: Entity, old: Dimension, entity_after: Entity, new: Dimension) -> bool:
    """True when `new` is declared at a lower level than `old`, or masks less."""
    return rank(_level(entity_after, new)) < rank(_level(entity_before, old)) or _mask_loosened(
        old.masking, new.masking
    )


def _columns(dimension: Dimension) -> frozenset[str]:
    """The columns a dimension reads, parsed (not the text of its expression). A dimension whose
    expression does not parse reads nothing anyone can name, so it matches no other."""
    try:
        return frozenset(c.lower() for c in dimension_columns(dimension))
    except ExpressionError:
        return frozenset({f"?{dimension.name}"})


def _mask_of(dimension: Dimension) -> tuple[int, float]:
    return _mask_strength(dimension.masking) if dimension.masking is not None else (0, 0.0)


def _sibling_findings(official: Entity, proposal: Entity) -> list[str]:
    """Each new or changed dimension, against the strictest official dimension that reads the same
    parsed columns: it may not show those columns at a lower level or with less masking. This is
    what a sibling (`birth_month` next to a masked `birth_year`), a swap of names, and a dimension
    re-pointed at a protected column have in common."""
    found: list[str] = []
    before = {d.name: d for d in official.dimensions}
    for new in proposal.dimensions:
        columns = _columns(new)
        old = before.get(new.name)
        if old is not None and _columns(old) == columns:
            continue  # same name, same columns: the comparison by name has judged it
        same = [o for o in official.dimensions if _columns(o) == columns]
        if not same:
            continue
        level = max((_level(official, o) for o in same), key=rank)
        mask = max(_mask_of(o) for o in same)
        if rank(_level(proposal, new)) < rank(level) or _mask_of(new) < mask:
            strictest = max(same, key=lambda o: (rank(_level(official, o)), _mask_of(o)))
            found.append(
                f"dimension {new.name} shows {', '.join(sorted(columns))} less strictly than "
                f"dimension {strictest.name} does"
            )
    return found


def loosenings(official: AnyDefinition | None, proposal: AnyDefinition) -> list[str]:
    """What in `proposal` the governance team must decide (D-20), as short sentences without any
    value from the definition: a less strict level, a removed or finer masking rule, a removed
    personal or masked dimension, a rename that weakens one, a new object with a personal
    dimension. Empty when none.

    Dimensions are matched by name, and a dimension that is gone is matched with a new one that
    reads the same expression (`expr`, or the column named like it): that is a rename, and it is
    compared like an edit. A dimension that is gone with no such match is a removal. Levels the
    catalog derives for columns are not part of a definition and are not compared here.
    """
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
        after = {d.name: d for d in proposal.dimensions}
        matched_new: set[str] = set()
        gone: list[Dimension] = []
        for old in official.dimensions:
            new = after.get(old.name)
            if new is None:
                gone.append(old)
                continue
            matched_new.add(new.name)
            if rank(_level(proposal, new)) < rank(_level(official, old)):
                found.append(f"a lower sensitivity for dimension {new.name}")
            if _mask_loosened(old.masking, new.masking):
                found.append(f"a removed or finer masking rule for dimension {new.name}")
        added = [
            d
            for d in proposal.dimensions
            if d.name not in matched_new and d.name not in {o.name for o in official.dimensions}
        ]
        for old in gone:
            twin = next((d for d in added if _expr_key(d) == _expr_key(old)), None)
            if twin is not None:
                added.remove(twin)
                if _weaker(official, old, proposal, twin):
                    found.append(
                        f"dimension {old.name} renamed to {twin.name} with a lower level "
                        "or less masking"
                    )
            elif _level(official, old) == "personal" or old.masking is not None:
                found.append(f"the removal of dimension {old.name}, which is personal or masked")
        for new in added:
            # New with no earlier version: compared with what the entity was.
            if rank(_level(proposal, new)) < rank(official.sensitivity):
                found.append(f"a new dimension {new.name} at a lower level than the entity")
        for line in _sibling_findings(official, proposal):
            if line not in found:
                found.append(line)
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
