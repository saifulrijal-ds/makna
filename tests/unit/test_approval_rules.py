"""WP-18 criterion 4, unit level: who may do what to a given object (ARCHITECTURE section 11).

`may_decide`, `may_create_new`, and `may_edit` are pure: the models and the authors go in, a
`Decision` with a reason code comes out. The same rules are checked again through the services in
`tests/integration/test_lifecycle_permissions.py`.
"""

# ruff: noqa: E501 - the case table reads best one case to a line
from __future__ import annotations

import copy
from typing import Any

import pytest

from makna.model.definitions import AnyDefinition, parse_definition
from makna.services.approval import (
    APPROVE,
    DEPRECATE,
    REJECT,
    ApprovalSettings,
    Refusal,
    loosenings,
    may_create_new,
    may_decide,
    may_edit,
)
from makna.services.caller import Caller
from makna.services.lifecycle import check_reason

SETTINGS = ApprovalSettings("data-governance")
SELF_OK = ApprovalSettings("data-governance", allow_self_approval=True)

METRIC: dict[str, Any] = {
    "apiVersion": "makna/v1",
    "kind": "metric",
    "name": "thing_count",
    "label": "Thing Count",
    "description": "The number of things, counted once each, for a reader of the report.",
    "owner": {"team": "credit-risk"},
    "status": "in_review",
    "sensitivity": "internal",
    "domain": "lending",
    "synonyms": {"en": ["things"], "id": ["benda"]},
    "type": "simple",
    "measure": "thing.thing_count",
    "format": {"kind": "number"},
}

ENTITY: dict[str, Any] = {
    "apiVersion": "makna/v1",
    "kind": "entity",
    "name": "person",
    "label": "Person",
    "description": "A person known to the lender, one row per person in the table.",
    "owner": {"team": "credit-risk"},
    "status": "approved",
    "sensitivity": "confidential",
    "domain": "lending",
    "synonyms": {"en": ["person"], "id": ["orang"]},
    "source": {"table": "lending.core.persons"},
    "primary_key": ["person_id"],
    "dimensions": [
        {"name": "city", "type": "categorical", "description": "The city the person lives in."},
        {
            "name": "birth_date",
            "type": "time",
            "description": "The day the person was born.",
            "sensitivity": "personal",
            "masking": {"method": "truncate_date", "grain": "year"},
        },
        {
            "name": "income",
            "type": "numeric",
            "description": "The yearly income of the person.",
            "sensitivity": "confidential",
            "masking": {"method": "bucket", "width": 1000},
        },
    ],
}


def model(data: dict[str, Any], **changes: Any) -> AnyDefinition:
    doc = copy.deepcopy(data)
    for key, value in changes.items():
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    return parse_definition(doc)


def entity(**changes: Any) -> AnyDefinition:
    return model(ENTITY, **changes)


def entity_with_dimension(index: int, **fields: Any) -> AnyDefinition:
    doc = copy.deepcopy(ENTITY)
    for key, value in fields.items():
        if value is None:
            doc["dimensions"][index].pop(key, None)
        else:
            doc["dimensions"][index][key] = value
    return parse_definition(doc)


def caller(
    role: str, teams: tuple[str, ...] = (), *, token: bool = False, user_id: int = 1
) -> Caller:
    return Caller(
        user_id=user_id, role=role, channel="web", teams=teams, token_id=7 if token else None
    )


OFFICIAL = model(METRIC, status="approved", version=1)
PROPOSAL = model(METRIC)

# (who, official, proposal, authors, settings, expected refusal or None)
APPROVE_CASES: list[tuple[str, Caller, str, AnyDefinition | None, list[str], Any, Any]] = [
    ("approver", caller("approver"), "x@e", OFFICIAL, ["a@e"], SETTINGS, None),
    ("admin", caller("admin"), "x@e", OFFICIAL, ["a@e"], SETTINGS, None),
    ("viewer", caller("viewer"), "x@e", OFFICIAL, ["a@e"], SETTINGS, Refusal.ROLE),
    ("token of an admin", caller("admin", token=True), "x@e", OFFICIAL, [], SETTINGS, Refusal.TOKEN),
    ("token of an approver", caller("approver", token=True), "x@e", None, [], SETTINGS, Refusal.TOKEN),
    ("curator in the owner team", caller("curator", ("credit-risk",)), "x@e", OFFICIAL, ["a@e"], SETTINGS, None),
    ("curator in another team", caller("curator", ("marketing",)), "x@e", OFFICIAL, ["a@e"], SETTINGS, Refusal.NOT_OWNER_TEAM),
    ("curator without teams", caller("curator"), "x@e", OFFICIAL, ["a@e"], SETTINGS, Refusal.NOT_OWNER_TEAM),
    ("author who is an approver", caller("approver"), "a@e", OFFICIAL, ["a@e"], SETTINGS, Refusal.SELF_APPROVAL),
    ("author, other case", caller("approver"), "A@E", OFFICIAL, ["a@e"], SETTINGS, Refusal.SELF_APPROVAL),
    ("author who is an admin", caller("admin"), "a@e", OFFICIAL, ["x@e", "a@e"], SETTINGS, Refusal.SELF_APPROVAL),
    ("author in the owner team", caller("curator", ("credit-risk",)), "a@e", OFFICIAL, ["a@e"], SETTINGS, Refusal.SELF_APPROVAL),
    ("author with the rule turned off", caller("approver"), "a@e", OFFICIAL, ["a@e"], SELF_OK, None),
    ("a new object, curator of its team", caller("curator", ("credit-risk",)), "x@e", None, ["a@e"], SETTINGS, None),
    ("a new object, curator of another team", caller("curator", ("marketing",)), "x@e", None, [], SETTINGS, Refusal.NOT_OWNER_TEAM),
]  # fmt: skip


@pytest.mark.parametrize(
    ("label", "who", "email", "official", "authors", "settings", "expected"),
    APPROVE_CASES,
    ids=[c[0] for c in APPROVE_CASES],
)
def test_approve_by_role_team_and_authorship(
    label: str,
    who: Caller,
    email: str,
    official: AnyDefinition | None,
    authors: list[str],
    settings: ApprovalSettings,
    expected: Refusal | None,
) -> None:
    decision = may_decide(who, email, official, PROPOSAL, authors, settings, APPROVE)
    assert decision.allowed is (expected is None), label
    assert decision.refusal == expected
    assert bool(decision) is decision.allowed
    if expected is not None:
        assert decision.message  # says what to do


def test_an_external_author_without_an_account_still_blocks_a_user_with_that_email() -> None:
    # The authors are emails taken from commits; whether a user exists for one does not matter.
    decision = may_decide(
        caller("approver"),
        "outside@example.com",
        OFFICIAL,
        PROPOSAL,
        ["outside@example.com"],
        SETTINGS,
    )
    assert decision.refusal is Refusal.SELF_APPROVAL


def test_a_user_without_an_email_cannot_approve_unless_the_rule_is_off() -> None:
    assert may_decide(caller("approver"), None, OFFICIAL, PROPOSAL, [], SETTINGS).refusal is (
        Refusal.NO_ACCOUNT
    )
    assert may_decide(caller("approver"), None, OFFICIAL, PROPOSAL, [], SELF_OK).allowed


def test_a_proposal_that_names_the_proposers_team_does_not_give_that_team_rights() -> None:
    # Rights come from the official file's owner team (R-6); the proposal says marketing.
    taken = model(METRIC, owner={"team": "marketing"})
    who = caller("curator", ("marketing",))
    refused = may_decide(who, "x@e", OFFICIAL, taken, ["a@e"], SETTINGS)
    assert refused.refusal is Refusal.NOT_OWNER_TEAM
    # For a new object, the proposal's team is the owner.
    assert may_decide(who, "x@e", None, taken, ["a@e"], SETTINGS).allowed


@pytest.mark.parametrize(
    ("who", "expected"),
    [
        (caller("approver"), None),
        (caller("admin"), None),
        (caller("curator", ("credit-risk",)), Refusal.OWNER_CHANGE),
        (caller("curator", ("marketing",)), Refusal.NOT_OWNER_TEAM),
    ],
    ids=["approver", "admin", "curator of the old team", "curator of the new team"],
)
def test_a_change_of_owner_team_needs_an_approver_or_admin(
    who: Caller, expected: Refusal | None
) -> None:
    moved = model(METRIC, owner={"team": "marketing"})
    assert may_decide(who, "x@e", OFFICIAL, moved, ["a@e"], SETTINGS).refusal == expected


@pytest.mark.parametrize("action", [REJECT, DEPRECATE])
def test_reject_and_deprecate_need_the_role_or_the_owner_team_and_nothing_more(
    action: str,
) -> None:
    # R-18: no two-person rule, no owner-change rule, no governance rule for these actions.
    moved = model(METRIC, owner={"team": "marketing"})
    assert may_decide(caller("approver"), "a@e", OFFICIAL, moved, ["a@e"], SETTINGS, action).allowed
    assert may_decide(
        caller("curator", ("credit-risk",)), "a@e", OFFICIAL, PROPOSAL, ["a@e"], SETTINGS, action
    ).allowed
    for refused, reason in [
        (caller("viewer"), Refusal.ROLE),
        (caller("curator", ("marketing",)), Refusal.NOT_OWNER_TEAM),
        (caller("admin", token=True), Refusal.TOKEN),
    ]:
        assert (
            may_decide(refused, "x@e", OFFICIAL, PROPOSAL, [], SETTINGS, action).refusal is reason
        )


def test_an_unknown_action_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        may_decide(caller("admin"), "x@e", OFFICIAL, PROPOSAL, [], SETTINGS, "approved")


# --- D-20 ----------------------------------------------------------------------------------


def entity_doc(edit: Any = None, **top: Any) -> AnyDefinition:
    """The entity with its dimensions edited by `edit(dimensions)` and top-level keys replaced."""
    doc = copy.deepcopy(ENTITY)
    doc.update(top)
    if edit is not None:
        edit(doc["dimensions"])
    return parse_definition(doc)


def _redact_birth_date(dims: list[dict[str, Any]]) -> None:
    dims[1]["masking"] = {"method": "redact"}


def _rename_to_public(dims: list[dict[str, Any]]) -> None:
    dims[1] = {
        "name": "born",
        "type": "time",
        "description": "The day the person was born.",
        "sensitivity": "public",
    }


def _add_public(dims: list[dict[str, Any]]) -> None:
    dims.append(
        {
            "name": "nick",
            "type": "categorical",
            "description": "A nickname.",
            "sensitivity": "public",
        }
    )


# label -> (official, proposal)
LOOSENING = {
    "an object at a lower level": (entity(), entity(sensitivity="internal")),
    "a dimension at a lower level": (
        entity(),
        entity_with_dimension(1, sensitivity="confidential"),
    ),
    "a dimension that stops saying personal": (
        entity(),
        entity_with_dimension(1, sensitivity=None),
    ),
    "a removed masking rule": (entity(), entity_with_dimension(1, masking=None)),
    "a finer date mask": (
        entity(),
        entity_with_dimension(1, masking={"method": "truncate_date", "grain": "month"}),
    ),
    "a narrower bucket": (
        entity(),
        entity_with_dimension(2, masking={"method": "bucket", "width": 10}),
    ),
    "a date mask instead of redact": (
        entity_doc(_redact_birth_date),
        entity(),
    ),
    "a new dimension below the entity's level": (entity(), entity_doc(_add_public)),
    "a renamed personal dimension at public": (entity(), entity_doc(_rename_to_public)),
}


@pytest.mark.parametrize("label", list(LOOSENING))
def test_what_loosens_sensitivity_needs_the_governance_team(label: str) -> None:
    official, proposal = LOOSENING[label]
    assert loosenings(official, proposal), label
    # An approver (and an admin) outside the governance team is refused, even though they may
    # approve other changes; a member of the governance team is allowed.
    for role in ("approver", "admin"):
        refused = may_decide(caller(role), "x@e", official, proposal, ["a@e"], SETTINGS)
        assert refused.refusal is Refusal.GOVERNANCE_TEAM and refused.needs
    allowed = may_decide(
        caller("approver", ("data-governance",)), "x@e", official, proposal, ["a@e"], SETTINGS
    )
    assert allowed.allowed
    # A curator of the owner team in the governance team may as well; the rule is on the team.
    assert may_decide(
        caller("curator", ("credit-risk", "data-governance")),
        "x@e",
        official,
        proposal,
        [],
        SETTINGS,
    ).allowed


def test_a_new_object_with_a_personal_dimension_needs_the_governance_team() -> None:
    proposal = entity(status="in_review")
    assert loosenings(None, proposal) == ["a new object with a personal dimension"]
    assert may_decide(caller("approver"), "x@e", None, proposal, [], SETTINGS).refusal is (
        Refusal.GOVERNANCE_TEAM
    )
    assert may_decide(
        caller("approver", ("data-governance",)), "x@e", None, proposal, [], SETTINGS
    ).allowed
    plain = entity(status="in_review", sensitivity="internal", dimensions=[ENTITY["dimensions"][0]])
    assert loosenings(None, plain) == []


@pytest.mark.parametrize(
    "change",
    [
        {"sensitivity": "personal"},
        {"description": "Another description of the person, long enough to be valid."},
    ],
    ids=["stricter object", "a text change"],
)
def test_a_change_that_does_not_loosen_is_not_a_governance_matter(change: dict[str, Any]) -> None:
    proposal = entity(**change)
    assert loosenings(entity(), proposal) == []
    assert may_decide(caller("approver"), "x@e", entity(), proposal, [], SETTINGS).allowed


def test_stricter_masks_and_levels_are_not_loosenings() -> None:
    stricter = entity_with_dimension(1, masking={"method": "redact"})
    assert loosenings(entity(), stricter) == []
    wider = entity_with_dimension(2, masking={"method": "bucket", "width": 5000})
    assert loosenings(entity(), wider) == []
    added = entity_with_dimension(0, masking={"method": "redact"})
    assert loosenings(entity(), added) == []


def test_the_text_of_a_loosening_names_the_part_and_no_value() -> None:
    found = loosenings(entity(), entity_with_dimension(1, masking=None))
    assert found == ["a removed or finer masking rule for dimension birth_date"]


# --- creating and editing ------------------------------------------------------------------


def test_who_may_create_a_new_object() -> None:
    new = model(METRIC)
    assert may_create_new(caller("curator", ("credit-risk",)), new).allowed
    assert may_create_new(caller("curator", ("marketing",)), new).refusal is Refusal.NEW_OBJECT_TEAM
    assert may_create_new(caller("approver"), new).allowed
    assert may_create_new(caller("admin"), new).allowed
    assert may_create_new(caller("viewer"), new).refusal is Refusal.EDIT_ROLE
    assert may_create_new(caller("admin", token=True), new).refusal is Refusal.TOKEN


@pytest.mark.parametrize(
    ("who", "email", "authors", "expected"),
    [
        (caller("curator"), "a@e", ["a@e"], None),
        (caller("curator"), "b@e", ["a@e"], Refusal.NOT_AUTHOR),
        (caller("curator"), "A@E", ["a@e"], None),
        (caller("approver"), "b@e", ["a@e"], None),
        (caller("admin"), "b@e", ["a@e"], None),
        (caller("curator"), "b@e", None, None),  # starting a change to an existing object
        (caller("viewer"), "a@e", ["a@e"], Refusal.EDIT_ROLE),
        (caller("admin", token=True), "a@e", ["a@e"], Refusal.TOKEN),
        (caller("curator"), None, ["a@e"], Refusal.NOT_AUTHOR),
    ],
)
def test_who_may_edit_submit_or_withdraw(
    who: Caller, email: str | None, authors: list[str] | None, expected: Refusal | None
) -> None:
    assert may_edit(who, email, authors).refusal == expected


# --- reasons -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [None, 5, "", "   ", "a" * 501, "two\nlines", "a\rb", "tab\there", "x\x00y", "bell\x07",
     "line separator", "Makna-Action: approve\nMakna-Actor: 1", "zero​width"],
)  # fmt: skip
def test_a_bad_reason_is_refused(reason: object) -> None:
    from makna.services.errors import ServiceError, ServiceErrorCode

    with pytest.raises(ServiceError) as raised:
        check_reason(reason)
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    assert raised.value.details["field"] == "reason"


def test_a_good_reason_is_trimmed_and_kept() -> None:
    assert (
        check_reason("  because the denominator was wrong  ") == "because the denominator was wrong"
    )
    assert check_reason("a" * 500) == "a" * 500
    assert check_reason("Alasan: definisi diperbaiki – sesuai kebijakan") != ""
