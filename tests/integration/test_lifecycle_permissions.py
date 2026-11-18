"""WP-18 criterion 4 through the services: every write cell of the permission matrix and the rules
below it (owner team, two-person rule, owner change, governance team, D-20).

The pure rules are checked cell by cell in `tests/unit/test_approval_rules.py`; here each case
goes through the lifecycle actions, so that the check is made on the state the lock protects and
a refusal leaves no commit and no audit row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import URL

from makna.services import ServiceError, ServiceErrorCode
from makna.services.errors import LifecycleErrorCode

from . import lifecycle_support as sup
from .lifecycle_support import World

METRIC = "metric:par30_ratio"  # owned by credit-risk
SALES_METRIC = "metric:disbursement_amount"  # owned by sales-performance
CUSTOMER = "entity:customer"  # owned by data-governance; birth_year is personal and masked
NEW = "metric:contract_count_total"


@pytest.fixture
def world(empty_database: URL, tmp_path: Path) -> Iterator[World]:
    built = sup.build_world(empty_database, tmp_path / "definitions")
    yield built
    built.services.dispose()


@pytest.fixture
def lenient_world(empty_database: URL, tmp_path: Path) -> Iterator[World]:
    built = sup.build_world(empty_database, tmp_path / "definitions", allow_self_approval=True)
    yield built
    built.services.dispose()


def relabel(world: World, object_id: str, label: str, who: str = "curator") -> None:
    text = sup.edited(sup.official_text(world, object_id), lambda d: d.update(label=label))
    world.life.create_proposal(world.caller(who), object_id, text, f"Rename to {label}")


def refused(world: World, call: Callable[[], Any], reason: str) -> None:
    """The call is refused as PERMISSION_DENIED with the reason code, and nothing changed."""
    head, audit = sup.head(world), sup.audit_rows(world.engine)
    with pytest.raises(ServiceError) as raised:
        call()
    assert raised.value.code == ServiceErrorCode.PERMISSION_DENIED, raised.value
    assert raised.value.details["reason"] == reason, raised.value.details
    assert sup.head(world) == head and sup.audit_rows(world.engine) == audit


# --- viewers and tokens ---------------------------------------------------------------------


def test_a_viewer_and_a_token_cannot_approve_a_proposal_that_others_wrote(world: World) -> None:
    relabel(world, METRIC, "Par 30 Ratio Renamed")
    world.life.submit(world.caller("curator"), METRIC, "ready")
    refused(
        world, lambda: world.life.approve(world.caller("viewer"), METRIC, "r"), "role_cannot_decide"
    )
    for name in ("admin", "approver", "curator2"):
        refused(
            world,
            lambda name=name: world.life.approve(world.token(name), METRIC, "r"),  # type: ignore[misc]
            "token_cannot_write",
        )
    refused(
        world, lambda: world.life.reject(world.token("admin"), METRIC, "r"), "token_cannot_write"
    )
    refused(
        world, lambda: world.life.withdraw(world.token("admin"), METRIC, "r"), "token_cannot_write"
    )
    refused(
        world, lambda: world.life.deprecate(world.token("admin"), METRIC, "r"), "token_cannot_write"
    )
    refused(
        world,
        lambda: world.life.deprecate(world.caller("viewer"), METRIC, "r"),
        "role_cannot_decide",
    )
    assert sup.object_row(world, METRIC)["version"] == 1  # nothing was approved


# --- owner team and authors -----------------------------------------------------------------


def test_who_may_approve_by_team_and_authorship(world: World) -> None:
    relabel(world, METRIC, "Par 30 Ratio Renamed", who="curator")
    world.life.submit(world.caller("curator"), METRIC, "ready")
    approve = world.life.approve
    # A curator outside the owner team cannot, in or out of any team of the proposal.
    refused(world, lambda: approve(world.caller("outsider"), METRIC, "r"), "not_in_owner_team")
    refused(world, lambda: approve(world.caller("sales"), METRIC, "r"), "not_in_owner_team")
    # The author cannot, even in the owner team, even as an approver's equal.
    refused(world, lambda: approve(world.caller("curator"), METRIC, "r"), "author_cannot_approve")
    # A curator of the owner team who did not write it can.
    result = approve(world.caller("curator2"), METRIC, "Approved by the owner team")
    assert (result.status, result.version) == ("approved", 2)


def test_no_author_of_any_commit_on_the_proposal_can_approve_it(world: World) -> None:
    # curator writes, approver edits last, approver2 submits: all three are authors.
    relabel(world, METRIC, "Par 30 Ratio Renamed", who="curator")
    edited = sup.edited(
        sup.official_text(world, METRIC), lambda d: d.update(label="Par 30 Ratio Edited")
    )
    world.life.update_proposal(world.caller("approver"), METRIC, edited, "edited by an approver")
    world.life.submit(world.caller("approver2"), METRIC, "submitted by another approver")
    for name in ("curator", "approver", "approver2"):
        refused(
            world,
            lambda name=name: world.life.approve(world.caller(name), METRIC, "r"),  # type: ignore[misc]
            "author_cannot_approve",
        )
    # A user nobody wrote with may approve: the admin, or a curator of the owner team.
    assert world.life.approve(world.caller("admin"), METRIC, "Fine").version == 2


def test_the_authors_start_again_after_an_approval(world: World) -> None:
    relabel(world, METRIC, "First Rename", who="curator")
    world.life.submit(world.caller("curator"), METRIC, "ready")
    world.life.approve(world.caller("approver"), METRIC, "ok")
    # curator wrote the earlier change; it is approved now, so the curator may approve a new one
    # that approver wrote, and approver (who approved, not wrote) may not approve their own.
    text = sup.edited(sup.official_text(world, METRIC), lambda d: d.update(label="Second Rename"))
    world.life.create_proposal(world.caller("approver"), METRIC, text, "a second change")
    world.life.submit(world.caller("approver"), METRIC, "ready")
    refused(
        world,
        lambda: world.life.approve(world.caller("approver"), METRIC, "r"),
        "author_cannot_approve",
    )
    assert world.life.approve(world.caller("curator"), METRIC, "ok").version == 3


def test_the_author_can_approve_when_the_rule_is_turned_off(lenient_world: World) -> None:
    world = lenient_world
    relabel(world, METRIC, "Par 30 Ratio Renamed", who="curator")
    world.life.submit(world.caller("curator"), METRIC, "ready")
    assert world.life.approve(world.caller("curator"), METRIC, "my own change").version == 2
    # The rule is the only thing turned off: a viewer, a token, and a curator of another team
    # still cannot approve.
    relabel(world, METRIC, "Par 30 Again", who="curator")
    world.life.submit(world.caller("curator"), METRIC, "ready")
    refused(
        world, lambda: world.life.approve(world.caller("viewer"), METRIC, "r"), "role_cannot_decide"
    )
    refused(
        world, lambda: world.life.approve(world.token("admin"), METRIC, "r"), "token_cannot_write"
    )
    refused(
        world,
        lambda: world.life.approve(world.caller("outsider"), METRIC, "r"),
        "not_in_owner_team",
    )


def test_a_proposal_naming_the_proposers_team_gives_that_team_no_rights_over_the_object(
    world: World,
) -> None:
    text = sup.edited(
        sup.official_text(world, SALES_METRIC), lambda d: d.update(owner={"team": "marketing"})
    )
    world.life.create_proposal(world.caller("outsider"), SALES_METRIC, text, "take over the metric")
    world.life.submit(world.caller("outsider"), SALES_METRIC, "ready")
    # Another curator of the proposer's team: rights come from the official file's team.
    refused(
        world,
        lambda: world.life.approve(world.caller("outsider2"), SALES_METRIC, "r"),
        "not_in_owner_team",
    )
    # A change of owner team can be approved only by an approver or admin, not by a curator of
    # the current owner team.
    refused(
        world,
        lambda: world.life.approve(world.caller("sales"), SALES_METRIC, "r"),
        "owner_change_needs_approver",
    )
    result = world.life.approve(world.caller("approver"), SALES_METRIC, "The metric moves")
    assert result.version == 2
    assert sup.object_row(world, SALES_METRIC)["owner_team"] == "marketing"


def test_a_change_of_owner_team_can_be_approved_by_an_admin(world: World) -> None:
    text = sup.edited(
        sup.official_text(world, SALES_METRIC), lambda d: d.update(owner={"team": "marketing"})
    )
    world.life.create_proposal(world.caller("outsider"), SALES_METRIC, text, "move")
    world.life.submit(world.caller("outsider"), SALES_METRIC, "ready")
    assert world.life.approve(world.caller("admin"), SALES_METRIC, "ok").version == 2


def test_a_curator_cannot_create_a_new_object_for_a_team_they_are_not_in(world: World) -> None:
    other_team = sup.metric_text(owner={"team": "marketing"})
    refused(
        world,
        lambda: world.life.create_proposal(world.caller("curator"), NEW, other_team, "r"),
        "new_object_team_not_yours",
    )
    own_team = sup.metric_text(owner={"team": "credit-risk"})
    assert world.life.create_proposal(world.caller("curator"), NEW, own_team, "r").status == "draft"
    # An approver or admin may create for any team.
    other = "metric:contracts_for_marketing"
    text = sup.metric_text(name="contracts_for_marketing", owner={"team": "marketing"})
    assert world.life.create_proposal(world.caller("approver"), other, text, "r").status == "draft"


def test_only_an_author_or_an_approver_may_edit_submit_or_withdraw(world: World) -> None:
    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "first")
    other = world.caller("curator2")
    refused(world, lambda: world.life.submit(other, NEW, "r"), "not_the_author")
    refused(
        world,
        lambda: world.life.update_proposal(other, NEW, sup.metric_text(label="X"), "r"),
        "not_the_author",
    )
    refused(world, lambda: world.life.withdraw(other, NEW, "r"), "not_the_author")
    refused(world, lambda: world.life.submit(world.caller("viewer"), NEW, "r"), "role_cannot_edit")
    assert world.life.update_proposal(
        world.caller("approver"), NEW, sup.metric_text(label="By An Approver"), "fix"
    )
    assert world.life.withdraw(world.caller("admin"), NEW, "no longer needed")


def test_reject_needs_the_role_or_the_owner_team_and_not_a_second_person(world: World) -> None:
    relabel(world, METRIC, "Par 30 Ratio Renamed", who="curator")
    world.life.submit(world.caller("curator"), METRIC, "ready")
    refused(
        world, lambda: world.life.reject(world.caller("outsider"), METRIC, "r"), "not_in_owner_team"
    )
    refused(
        world, lambda: world.life.reject(world.caller("viewer"), METRIC, "r"), "role_cannot_decide"
    )
    # The author, a curator of the owner team, may reject their own proposal (R-18).
    assert world.life.reject(world.caller("curator"), METRIC, "I found a mistake").status == "draft"


def test_deprecate_needs_the_role_or_the_owner_team(world: World) -> None:
    refused(
        world,
        lambda: world.life.deprecate(world.caller("outsider"), METRIC, "r"),
        "not_in_owner_team",
    )
    refused(
        world, lambda: world.life.deprecate(world.caller("sales"), METRIC, "r"), "not_in_owner_team"
    )
    assert world.life.deprecate(world.caller("curator"), METRIC, "retired").status == "deprecated"


# --- D-20 -----------------------------------------------------------------------------------


def _unmask_birth_year(data: dict[str, Any]) -> None:
    for dimension in data["dimensions"]:
        if dimension["name"] == "birth_year":
            dimension.pop("masking")


def _finer_mask(data: dict[str, Any]) -> None:
    for dimension in data["dimensions"]:
        if dimension["name"] == "birth_year":
            dimension["masking"] = {"method": "truncate_date", "grain": "month"}


def _lower_level(data: dict[str, Any]) -> None:
    for dimension in data["dimensions"]:
        if dimension["name"] == "birth_year":
            dimension["sensitivity"] = "confidential"


@pytest.mark.parametrize(
    "edit",
    [_unmask_birth_year, _finer_mask, _lower_level],
    ids=["removes a masking rule", "finer masking", "lower dimension level"],
)
def test_loosening_sensitivity_needs_a_member_of_the_governance_team(
    world: World, edit: Callable[[dict[str, Any]], None]
) -> None:
    world.life.create_proposal(
        world.caller("curator"),
        CUSTOMER,
        sup.edited(sup.official_text(world, CUSTOMER), edit),
        "loosen",
    )
    world.life.submit(world.caller("curator"), CUSTOMER, "ready")
    # approver and admin may approve other changes, and are not enough for this one.
    for name in ("approver", "approver2", "admin"):
        refused(
            world,
            lambda name=name: world.life.approve(world.caller(name), CUSTOMER, "r"),  # type: ignore[misc]
            "governance_team_needed",
        )
    governance = world.caller("governance")
    if edit is _lower_level:
        # The governance team may decide it, but a column the catalog tags personal declared
        # lower is a reference issue (R03) of the proposal itself, and that blocks the approval.
        with pytest.raises(ServiceError) as blocked:
            world.life.approve(governance, CUSTOMER, "The governance team agrees")
        assert blocked.value.code == LifecycleErrorCode.OPEN_REFERENCE_ISSUE
        assert any(i.startswith("R03") for i in blocked.value.details["issues"])
    else:
        assert world.life.approve(governance, CUSTOMER, "The governance team agrees")


def test_a_stricter_change_does_not_need_the_governance_team(world: World) -> None:
    def stricter(data: dict[str, Any]) -> None:
        data["description"] = data["description"] + " More text for the same customer."

    world.life.create_proposal(
        world.caller("curator"),
        CUSTOMER,
        sup.edited(sup.official_text(world, CUSTOMER), stricter),
        "text",
    )
    world.life.submit(world.caller("curator"), CUSTOMER, "ready")
    assert world.life.approve(world.caller("approver"), CUSTOMER, "ok").version == 2


def test_a_new_object_with_a_personal_dimension_needs_the_governance_team(world: World) -> None:
    def renamed(data: dict[str, Any]) -> None:
        data.update(name="customer_copy", label="Customer Copy", owner={"team": "credit-risk"})

    object_id = "entity:customer_copy"
    text = sup.edited(sup.official_text(world, CUSTOMER), renamed)
    world.life.create_proposal(
        world.caller("curator"), object_id, text, "a copy with personal data"
    )
    world.life.submit(world.caller("curator"), object_id, "ready")
    refused(
        world,
        lambda: world.life.approve(world.caller("approver"), object_id, "r"),
        "governance_team_needed",
    )
    refused(
        world,
        lambda: world.life.approve(world.caller("admin"), object_id, "r"),
        "governance_team_needed",
    )
    approved = world.life.approve(world.caller("governance"), object_id, "Reviewed by governance")
    assert approved.status == "approved"


def test_a_refusal_names_what_needs_the_governance_team_and_no_value(world: World) -> None:
    world.life.create_proposal(
        world.caller("curator"),
        CUSTOMER,
        sup.edited(sup.official_text(world, CUSTOMER), _unmask_birth_year),
        "loosen",
    )
    world.life.submit(world.caller("curator"), CUSTOMER, "ready")
    with pytest.raises(ServiceError) as raised:
        world.life.approve(world.caller("approver"), CUSTOMER, "r")
    assert raised.value.details["needs"] == [
        "a removed or finer masking rule for dimension birth_year"
    ]
