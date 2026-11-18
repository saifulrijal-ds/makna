"""WP-18 criteria 1, 3, 5, 6, 7, and 8, and the rulings about paths, reasons, git, and plain
directories (integration tier, with a temporary git repository)."""

from __future__ import annotations

import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import URL

from makna.definitions.render import sha256
from makna.index.tables import audit_event, reference_issue
from makna.model.request import QueryError
from makna.services import ServiceError, ServiceErrorCode, Services, store
from makna.services.errors import LifecycleErrorCode
from tests.catalog_support import DEFINITIONS

from . import lifecycle_support as sup
from .lifecycle_support import World

NEW = "metric:contract_count_total"
NEW_PATH = "lending/metrics/contract_count_total.yaml"
NEW_DRAFT = "lending/metrics/contract_count_total.draft.yaml"
CHANGED = "metric:disbursement_amount"
CHANGED_PATH = "lending/metrics/disbursement_amount.yaml"


@pytest.fixture
def world(empty_database: URL, tmp_path: Path) -> Iterator[World]:
    built = sup.build_world(empty_database, tmp_path / "definitions")
    yield built
    built.services.dispose()


def propose_and_submit(world: World, text: str | None = None) -> None:
    curator = world.caller("curator")
    world.life.create_proposal(curator, NEW, text or sup.metric_text(), "a first draft")
    world.life.submit(curator, NEW, "ready for review")


def commits(world: World) -> list[Any]:
    return world.repo.log()


def audit_actions(world: World) -> list[tuple[str, str]]:
    return [(r["action"], r["object_id"]) for r in sup.audit_rows(world.engine)]


# --- criterion 1 -----------------------------------------------------------------------------


def test_draft_to_approved_makes_three_commits_by_the_acting_users(world: World) -> None:
    before = len(commits(world))
    audit_before = len(sup.audit_rows(world.engine))
    created = world.life.create_proposal(
        world.caller("curator"), NEW, sup.metric_text(), "Draft of the total contract count"
    )
    assert (created.status, created.version) == ("draft", None)
    assert world.repo.show(sup.head(world), NEW_DRAFT) is not None
    submitted = world.life.submit(world.caller("curator"), NEW, "Ready for review")
    assert submitted.status == "in_review"
    approved = world.life.approve(world.caller("approver"), NEW, "Matches the policy")
    assert (approved.status, approved.version) == ("approved", 1)

    head = sup.head(world)
    assert world.repo.show(head, NEW_DRAFT) is None  # no proposal file
    text_ = (world.repo.show(head, NEW_PATH) or b"").decode()
    assert "status: approved" in text_ and "version: 1" in text_
    new = commits(world)[before:]
    assert len(new) == 3
    assert [(c.author_name, c.author_email) for c in new] == [
        ("Curator", "curator@example.com"),
        ("Curator", "curator@example.com"),
        ("Approver", "approver@example.com"),
    ]
    reasons = ["Draft of the total contract count", "Ready for review", "Matches the policy"]
    verbs = ["propose", "submit", "approve"]
    actions = ["create_proposal", "submit", "approve"]
    for commit, reason, verb, action in zip(new, reasons, verbs, actions, strict=True):
        message = world.repo._git(["log", "-1", "--format=%B", commit.sha]).decode()
        lines = message.splitlines()
        assert lines[0] == f"{verb}({NEW}): {reason}"
        assert lines[1] == ""
        assert lines[2:6] == [
            f"Makna-Action: {action}",
            f"Makna-Object: {NEW}",
            f"Makna-Actor: {world.ids['approver' if action == 'approve' else 'curator']}",
            lines[5],
        ]
        assert lines[5].startswith("Makna-Version: ")
        committer = world.repo._git(["log", "-1", "--format=%cn", commit.sha]).decode().strip()
        assert committer == "Makna"
    rows = [r for r in sup.audit_rows(world.engine)[audit_before:]]
    assert [(r["action"], r["object_id"]) for r in rows] == [(a, NEW) for a in actions]
    assert [r["commit_sha"] for r in rows] == [c.sha for c in new]
    assert [r["reason"] for r in rows] == reasons
    assert [r["to_status"] for r in rows] == ["draft", "in_review", "approved"]
    assert [r["actor_user_id"] for r in rows] == [
        world.ids["curator"],
        world.ids["curator"],
        world.ids["approver"],
    ]
    assert sup.object_row(world, NEW)["official_commit"] == head
    assert sup.object_row(world, NEW)["version"] == 1


def test_the_file_written_is_the_checked_model_not_the_text_sent(world: World) -> None:
    # A curator's `status`, `version`, and comments never reach the file: the application sets them.
    text_ = sup.metric_text(status="approved", version=99) + "# a comment that is dropped\n"
    world.life.create_proposal(world.caller("curator"), NEW, text_, "tries to set the state")
    written = (world.repo.show(sup.head(world), NEW_DRAFT) or b"").decode()
    assert "status: draft" in written and "version" not in written and "comment" not in written
    assert sup.object_row(world, NEW)["status"] == "draft"


# --- the common refusals ---------------------------------------------------------------------


BAD_REASONS = [None, "", "   ", "line one\nline two", "x" * 501, "a\x00b"]


@pytest.mark.parametrize(
    "action", ["create", "update", "submit", "approve", "reject", "withdraw", "deprecate"]
)
def test_a_missing_or_bad_reason_is_refused_for_every_action(world: World, action: str) -> None:
    # Set the object up in a state where the action could otherwise work, then call with a bad
    # reason; nothing may be written, committed, or audited.
    if action != "create":
        world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "first")
        if action in ("approve", "reject"):
            world.life.submit(world.caller("curator"), NEW, "ready")
    target = CHANGED if action == "deprecate" else NEW
    who = "approver" if action in ("approve", "reject", "deprecate") else "curator"
    audit_before, head_before = sup.audit_rows(world.engine), sup.head(world)
    call: Callable[[Any, Any], Any] = {
        "create": lambda c, r: world.life.create_proposal(c, NEW, sup.metric_text(), r),
        "update": lambda c, r: world.life.update_proposal(
            c, NEW, sup.metric_text(label="Other"), r
        ),
        "submit": lambda c, r: world.life.submit(c, NEW, r),
        "approve": lambda c, r: world.life.approve(c, NEW, r),
        "reject": lambda c, r: world.life.reject(c, NEW, r),
        "withdraw": lambda c, r: world.life.withdraw(c, NEW, r),
        "deprecate": lambda c, r: world.life.deprecate(c, target, r),
    }[action]
    if action == "create":
        audit_before, head_before = sup.audit_rows(world.engine), sup.head(world)
    for bad in BAD_REASONS:
        with pytest.raises(ServiceError) as raised:
            call(world.caller(who), bad)
        assert raised.value.code == ServiceErrorCode.INVALID_INPUT
        assert raised.value.details["field"] == "reason"
    assert sup.audit_rows(world.engine) == audit_before
    assert sup.head(world) == head_before
    assert sup.git(world.repo, "status", "--porcelain") == ""


def test_a_reason_cannot_add_a_trailer_or_a_second_line(world: World) -> None:
    forged = "ok\n\nMakna-Action: approve\nMakna-Actor: 1"
    with pytest.raises(ServiceError):
        world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), forged)
    world.life.create_proposal(
        world.caller("curator"), NEW, sup.metric_text(), "Makna-Action: approve"
    )
    message = world.repo._git(["log", "-1", "--format=%B"]).decode()
    assert message.count("Makna-Action:") == 2  # the one in the subject line, and the real one
    assert message.splitlines()[0] == f"propose({NEW}): Makna-Action: approve"
    assert message.splitlines()[2] == "Makna-Action: create_proposal"


@pytest.mark.parametrize("who", ["viewer", "token"])
def test_viewers_and_tokens_cannot_write_anything(world: World, who: str) -> None:
    caller = world.token("admin") if who == "token" else world.caller("viewer")
    life = world.life
    calls: list[Callable[[], Any]] = [
        lambda: life.create_proposal(caller, NEW, sup.metric_text(), "r"),
        lambda: life.update_proposal(caller, NEW, sup.metric_text(), "r"),
        lambda: life.submit(caller, NEW, "r"),
        lambda: life.approve(caller, NEW, "r"),
        lambda: life.reject(caller, NEW, "r"),
        lambda: life.withdraw(caller, NEW, "r"),
        lambda: life.deprecate(caller, CHANGED, "r"),
    ]
    head, audit = sup.head(world), sup.audit_rows(world.engine)
    for call in calls:
        with pytest.raises(ServiceError) as raised:
            call()
        assert raised.value.code == ServiceErrorCode.PERMISSION_DENIED
    assert sup.head(world) == head and sup.audit_rows(world.engine) == audit


def test_bad_ids_and_bad_yaml_are_invalid_input_and_write_nothing(world: World) -> None:
    curator = world.caller("curator")
    head = sup.head(world)
    for object_id in ("metric:../evil", "metric:", "kind:x", "metric:A", "../../etc/passwd", 7):
        with pytest.raises(ServiceError) as raised:
            world.life.create_proposal(curator, object_id, sup.metric_text(), "r")
        assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    other_name = sup.metric_text(name="other_name")
    with pytest.raises(ServiceError) as raised:
        world.life.create_proposal(curator, NEW, other_name, "the name differs from the id")
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    with pytest.raises(ServiceError) as raised:  # an entity's fields are missing
        world.life.create_proposal(curator, NEW, sup.metric_text(kind="entity"), "wrong kind")
    assert raised.value.code == LifecycleErrorCode.DEFINITION_INVALID
    with pytest.raises(ServiceError) as raised:
        world.life.create_proposal(curator, NEW, sup.metric_text(domain="nowhere"), "no domain")
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    for bad_yaml in (
        "a: &x 1\nb: *x\n",
        "kind: metric\nname: contract_count_total\n",
        "- a\n- b\n",
        "x: !!python/object/apply:os.system ['true']\n",
    ):
        with pytest.raises(ServiceError) as raised:
            world.life.create_proposal(curator, NEW, bad_yaml, "r")
        assert raised.value.code == LifecycleErrorCode.DEFINITION_INVALID
        assert raised.value.details["issues"] and raised.value.details["issues"][0]["rule"] == "V01"
    big = sup.metric_text() + "# " + "x" * (256 * 1024) + "\n"
    with pytest.raises(ServiceError) as raised:
        world.life.create_proposal(curator, NEW, big, "too big")
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    assert sup.head(world) == head
    assert not [r for r in sup.audit_rows(world.engine) if r["action"] == "create_proposal"]


def test_the_path_comes_from_the_id_and_the_domain_never_from_the_text(world: World) -> None:
    # A name or domain that would make a path outside the repository cannot pass the model.
    curator = world.caller("curator")
    for field, value in (("name", "../../outside"), ("domain", "../x")):
        with pytest.raises(ServiceError):
            world.life.create_proposal(curator, NEW, sup.metric_text(**{field: value}), "r")
    assert not (world.repo.path.parent / "outside.draft.yaml").exists()
    assert sup.head(world) == world.repo.rev_parse("HEAD")


# --- criterion 3 -----------------------------------------------------------------------------


def test_a_change_to_an_approved_metric_is_served_only_after_approval(world: World) -> None:
    svc, viewer = world.services, world.caller("viewer", channel="mcp")
    token = world.token("admin")
    old = svc.get_definition(viewer, CHANGED)
    assert old.version == 1
    text_ = sup.edited(
        sup.official_text(world, CHANGED), lambda d: d.update(label="Disbursement Total")
    )
    world.life.create_proposal(world.caller("curator"), CHANGED, text_, "Rename the metric")
    world.life.submit(world.caller("curator"), CHANGED, "Please review")
    for caller in (viewer, token):
        shown = svc.get_definition(caller, CHANGED)
        assert (shown.version, shown.label) == (1, old.label)
    assert svc.compile_query(token, {"metric": "disbursement_amount"}).sql
    row = sup.object_row(world, CHANGED)
    assert row["has_proposal"] and row["proposal_status"] == "in_review"
    assert row["status"] == "approved" and row["version"] == 1

    approved = world.life.approve(world.caller("approver"), CHANGED, "Agreed")
    assert approved.version == 2
    for caller in (viewer, token):
        shown = svc.get_definition(caller, CHANGED)
        assert (shown.version, shown.label) == (2, "Disbursement Total")
    assert not sup.object_row(world, CHANGED)["has_proposal"]
    assert world.repo.show(sup.head(world), CHANGED_PATH.replace(".yaml", ".draft.yaml")) is None


# --- criterion 5 -----------------------------------------------------------------------------


def test_submit_is_refused_with_the_rule_codes_when_the_proposal_has_a_definition_error(
    world: World,
) -> None:
    bad = sup.metric_text(measure="contract_snapshot.no_such_measure")
    world.life.create_proposal(world.caller("curator"), NEW, bad, "a draft with an error")
    head, audit = sup.head(world), sup.audit_rows(world.engine)
    with pytest.raises(ServiceError) as raised:
        world.life.submit(world.caller("curator"), NEW, "try")
    error = raised.value
    assert error.code == LifecycleErrorCode.DEFINITION_INVALID
    assert {i["rule"] for i in error.details["issues"]} <= {f"V{n:02d}" for n in range(1, 30)}
    assert error.details["issues"][0]["line"] is not None
    assert sup.head(world) == head and sup.audit_rows(world.engine) == audit
    errors = sup.object_row(world, NEW)["proposal_errors"]
    assert errors and errors[0]["code"].startswith("V")  # the index keeps them for a review page


def test_a_stale_issue_on_the_official_version_does_not_block_a_proposal(world: World) -> None:
    # The ruling: approve checks the proposal's own content against the catalog. An open row of
    # `reference_issue` belongs to the version that is official now.
    propose_and_submit(world)
    with world.engine.begin() as connection:
        connection.execute(
            insert(reference_issue).values(
                object_id=NEW, part="measure", code="R02", message="a column is missing"
            )
        )
    assert world.life.approve(world.caller("approver"), NEW, "this one fixes it").status == (
        "approved"
    )


def test_a_proposal_that_would_have_a_reference_issue_is_refused(world: World) -> None:
    # A change that points an entity at a table the catalog does not have (R01).
    text = sup.edited(
        sup.official_text(world, "entity:branch"),
        lambda d: d.update(source={"table": "lending.core.no_such_table"}),
    )
    world.life.create_proposal(world.caller("curator"), "entity:branch", text, "a new table")
    world.life.submit(world.caller("curator"), "entity:branch", "ready")
    head = sup.head(world)
    with pytest.raises(ServiceError) as raised:
        world.life.approve(world.caller("approver"), "entity:branch", "approve anyway")
    assert raised.value.code == LifecycleErrorCode.OPEN_REFERENCE_ISSUE
    assert any(i.startswith("R01") for i in raised.value.details["issues"])
    assert sup.head(world) == head


def test_a_proposal_with_a_definition_error_is_not_approved_when_it_was_submitted_clean(
    world: World,
) -> None:
    propose_and_submit(world)
    assert world.life.approve(world.caller("approver"), NEW, "fine").status == "approved"


def test_an_outside_proposal_with_errors_cannot_be_approved(world: World) -> None:
    broken = sup.edited(
        sup.official_text(world, CHANGED), lambda d: d.update(measure="contract.no_such_measure")
    )
    path = CHANGED_PATH.replace(".yaml", ".draft.yaml")
    (world.repo.path / path).write_text(broken)
    sup.git(world.repo, "add", path)
    sup.git(world.repo, "commit", "-q", "-m", "a hand edit")
    world.life.sync()
    row = sup.object_row(world, CHANGED)
    assert row["proposal_status"] == "in_review" and row["proposal_errors"]
    head = sup.head(world)
    with pytest.raises(ServiceError) as raised:
        world.life.approve(world.caller("approver"), CHANGED, "looks fine to me")
    assert raised.value.code == LifecycleErrorCode.DEFINITION_INVALID
    assert sup.head(world) == head


def test_an_approval_that_would_break_the_official_set_is_refused_before_anything_is_written(
    world: World,
) -> None:
    # Deprecating a metric that an approved metric uses as an input would fail V13 in the build.
    head, audit = sup.head(world), sup.audit_rows(world.engine)
    with pytest.raises(ServiceError) as raised:
        world.life.deprecate(world.caller("approver"), "metric:npf_outstanding_principal", "retire")
    assert raised.value.code == LifecycleErrorCode.DEFINITION_INVALID
    assert any(i["rule"] == "V13" for i in raised.value.details["issues"])
    assert sup.head(world) == head and sup.audit_rows(world.engine) == audit


# --- criterion 6 -----------------------------------------------------------------------------


def test_reject_returns_the_proposal_to_draft_and_records_the_reason(world: World) -> None:
    propose_and_submit(world)
    result = world.life.reject(world.caller("approver"), NEW, "The grain is wrong")
    assert result.status == "draft"
    assert sup.object_row(world, NEW)["status"] == "draft"
    last = sup.audit_rows(world.engine)[-1]
    assert (last["action"], last["reason"], last["to_status"]) == (
        "reject",
        "The grain is wrong",
        "draft",
    )
    assert "status: draft" in (world.repo.show(sup.head(world), NEW_DRAFT) or b"").decode()
    assert world.repo._git(["log", "-1", "--format=%s"]).decode().strip() == (
        f"reject({NEW}): The grain is wrong"
    )
    with pytest.raises(ServiceError) as raised:  # a draft is not in review
        world.life.reject(world.caller("approver"), NEW, "again")
    assert raised.value.code == LifecycleErrorCode.CONFLICT


def test_withdraw_deletes_the_proposal_file(world: World) -> None:
    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "first")
    world.life.withdraw(world.caller("curator"), NEW, "no longer needed")
    assert world.repo.show(sup.head(world), NEW_DRAFT) is None
    assert not (world.repo.path / NEW_DRAFT).exists()
    assert not sup.has_object(world, NEW)
    assert sup.audit_rows(world.engine)[-1]["action"] == "withdraw"
    with pytest.raises(ServiceError) as raised:
        world.life.withdraw(world.caller("curator"), NEW, "again")
    assert raised.value.code == ServiceErrorCode.NOT_FOUND


def test_deprecate_hides_the_object_from_search_and_the_compiler_refuses_it(world: World) -> None:
    propose_and_submit(world)
    world.life.approve(world.caller("approver"), NEW, "ok")
    svc, token = world.services, world.token("admin")
    assert "metric:contract_count_total" in [
        h.id for h in svc.search(token, "total contract count").hits
    ]
    assert svc.compile_query(token, {"metric": "contract_count_total"}).sql

    result = world.life.deprecate(
        world.caller("approver2"), NEW, "Replaced by the disbursement metric", CHANGED
    )
    assert result.status == "deprecated"
    assert "metric:contract_count_total" not in [
        h.id for h in svc.search(token, "total contract count").hits
    ]
    shown = [h.id for h in svc.search(token, "total contract count", include_deprecated=True).hits]
    assert "metric:contract_count_total" in shown
    with pytest.raises(QueryError) as raised:
        svc.compile_query(token, {"metric": "contract_count_total"})
    assert raised.value.code == "DEPRECATED"
    definition = svc.get_definition(world.caller("viewer", "mcp"), NEW)
    assert definition.status == "deprecated"
    text_ = (world.repo.show(sup.head(world), NEW_PATH) or b"").decode()
    assert "status: deprecated" in text_ and "Replaced by the disbursement metric" in text_
    assert f"replaced_by: {CHANGED}" in text_
    last = sup.audit_rows(world.engine)[-1]
    assert (last["action"], last["from_status"], last["to_status"]) == (
        "deprecate",
        "approved",
        "deprecated",
    )


@pytest.mark.parametrize(
    "replacement",
    [
        "metric:contract_count_total",
        "entity:contract",
        "metric:write_off_amount",
        "metric:nope",
        "x",
    ],
)
def test_a_replacement_must_be_another_approved_object_of_the_same_kind(
    world: World, replacement: str
) -> None:
    propose_and_submit(world)
    world.life.approve(world.caller("approver"), NEW, "ok")
    with pytest.raises(ServiceError) as raised:
        world.life.deprecate(world.caller("approver"), NEW, "retire", replacement)
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    assert raised.value.details["field"] == "replaced_by"


def test_a_deprecated_object_cannot_be_changed_and_a_pending_proposal_cannot_revive_it(
    world: World,
) -> None:
    propose_and_submit(world)
    world.life.approve(world.caller("approver"), NEW, "ok")
    world.life.create_proposal(
        world.caller("curator"),
        NEW,
        sup.edited(sup.official_text(world, NEW), lambda d: d.update(label="Renamed")),
        "a change while it is current",
    )
    world.life.submit(world.caller("curator"), NEW, "ready")
    world.life.deprecate(world.caller("approver"), NEW, "retire it")
    with pytest.raises(ServiceError) as raised:
        world.life.approve(world.caller("approver2"), NEW, "approve the change")
    assert raised.value.code == LifecycleErrorCode.CONFLICT
    assert sup.object_row(world, NEW)["status"] == "deprecated"


# --- states ----------------------------------------------------------------------------------


def test_actions_need_the_right_state(world: World) -> None:
    curator, approver = world.caller("curator"), world.caller("approver")
    missing: tuple[Callable[[], Any], ...] = (
        lambda: world.life.submit(curator, NEW, "r"),
        lambda: world.life.approve(approver, NEW, "r"),
        lambda: world.life.update_proposal(curator, NEW, sup.metric_text(), "r"),
    )
    for missing_call in missing:
        with pytest.raises(ServiceError) as raised:
            missing_call()
        assert raised.value.code == ServiceErrorCode.NOT_FOUND
    world.life.create_proposal(curator, NEW, sup.metric_text(), "first")
    with pytest.raises(ServiceError) as raised:
        world.life.create_proposal(curator, NEW, sup.metric_text(), "again")
    assert raised.value.code == LifecycleErrorCode.CONFLICT
    with pytest.raises(ServiceError) as raised:  # still a draft
        world.life.approve(approver, NEW, "too early")
    assert raised.value.code == LifecycleErrorCode.CONFLICT
    world.life.submit(curator, NEW, "ready")
    with pytest.raises(ServiceError):
        world.life.submit(curator, NEW, "twice")
    # An edit of a proposal in review returns it to draft.
    world.life.update_proposal(curator, NEW, sup.metric_text(label="Contracts Total"), "changed")
    assert sup.object_row(world, NEW)["status"] == "draft"
    with pytest.raises(ServiceError) as raised:  # the same text again is no change
        world.life.update_proposal(curator, NEW, sup.metric_text(label="Contracts Total"), "same")
    assert raised.value.code == LifecycleErrorCode.CONFLICT


def test_deprecating_needs_an_approved_object(world: World) -> None:
    with pytest.raises(ServiceError) as raised:
        world.life.deprecate(world.caller("approver"), "metric:write_off_amount", "never approved")
    assert raised.value.code == LifecycleErrorCode.CONFLICT
    with pytest.raises(ServiceError) as raised:
        world.life.deprecate(world.caller("approver"), "metric:nothing_here", "r")
    assert raised.value.code == ServiceErrorCode.NOT_FOUND
    world.life.deprecate(
        world.caller("approver"), "metric:npl_ratio" if False else "metric:par30_ratio", "retired"
    )
    with pytest.raises(ServiceError) as raised:
        world.life.deprecate(world.caller("approver"), "metric:par30_ratio", "twice")
    assert raised.value.code == LifecycleErrorCode.CONFLICT


# --- criterion 7 -----------------------------------------------------------------------------


def make_second_proposal(world: World, name: str) -> str:
    object_id = f"metric:{name}"
    world.life.create_proposal(
        world.caller("curator"), object_id, sup.metric_text(name=name), f"proposal {name}"
    )
    world.life.submit(world.caller("curator"), object_id, "ready")
    return object_id


def test_two_approvals_at_the_same_time_make_two_commits_in_sequence(world: World) -> None:
    first = make_second_proposal(world, "contracts_one")
    second = make_second_proposal(world, "contracts_two")
    # A second `Services` on its own engine plays a second process: the lock is in the database.
    other_engine = create_engine(world.url)
    other = Services(other_engine, world.services._search._provider, policy=world.services.policy,
                     definitions_repo=str(world.repo.path),
                     approval=world.services._lifecycle._settings)  # fmt: skip
    barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}

    def run(life: Any, who: str, object_id: str) -> None:
        barrier.wait()
        try:
            outcomes[object_id] = life.approve(world.caller(who), object_id, "approved together")
        except Exception as error:  # noqa: BLE001 - the test reports it below
            outcomes[object_id] = error

    before = len(commits(world))
    threads = [
        threading.Thread(target=run, args=(world.life, "approver", first)),
        threading.Thread(target=run, args=(other.lifecycle, "approver2", second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    other.dispose()
    assert all(not isinstance(v, Exception) for v in outcomes.values()), outcomes
    new = commits(world)[before:]
    assert len(new) == 2 and new[1].parents == (new[0].sha,)  # in sequence, no lost commit
    assert {c.changes[0][1] for c in new} | {c.changes[1][1] for c in new} >= {
        "lending/metrics/contracts_one.yaml",
        "lending/metrics/contracts_two.yaml",
    }
    for object_id in (first, second):
        assert sup.object_row(world, object_id)["status"] == "approved"
        assert not sup.object_row(world, object_id)["has_proposal"]
    approvals = [r for r in sup.audit_rows(world.engine) if r["action"] == "approve"]
    assert {r["commit_sha"] for r in approvals} == {c.sha for c in new}
    with world.engine.connect() as connection:
        last = connection.execute(
            text("SELECT value FROM index_state WHERE key = 'last_indexed_commit'")
        ).scalar_one()
    assert last == sup.head(world)
    before_dump = sup.state_dump(world.engine)
    world.life.sync()
    assert sup.state_dump(world.engine) == before_dump  # nothing was left to repair


def test_two_approvals_of_the_same_proposal_make_one_commit(world: World) -> None:
    propose_and_submit(world)
    barrier = threading.Barrier(2)
    results: list[Any] = []

    def run(who: str) -> None:
        barrier.wait()
        try:
            results.append(world.life.approve(world.caller(who), NEW, "approved"))
        except ServiceError as error:
            results.append(error)

    threads = [threading.Thread(target=run, args=(w,)) for w in ("approver", "approver2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    done = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, ServiceError)]
    assert len(done) == 1 and len(refused) == 1  # the loser sees the state the winner left
    assert sup.object_row(world, NEW)["version"] == 1
    assert len([r for r in sup.audit_rows(world.engine) if r["action"] == "approve"]) == 1


def test_a_waiting_caller_holds_no_connection_and_gives_up_with_a_conflict(world: World) -> None:
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with store.advisory_lock(world.engine):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(10)
    try:
        started = cast(Any, world.engine.pool).checkedout()
        with pytest.raises(ServiceError) as raised:
            with store.advisory_lock(world.engine, wait_seconds=0.3):
                pass
        assert raised.value.code == LifecycleErrorCode.CONFLICT
        assert cast(Any, world.engine.pool).checkedout() == started  # nothing held while it waited
    finally:
        release.set()
        holder.join(10)
    with store.advisory_lock(world.engine, wait_seconds=1):  # free again
        pass


# --- criterion 8 -----------------------------------------------------------------------------


def test_recovery_completes_an_action_whose_index_transaction_failed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    propose_and_submit(world)
    with monkeypatch.context() as patched:
        patched.setattr(store, "reindex", _fail)
        with pytest.raises(ServiceError) as raised:
            world.life.approve(world.caller("approver"), NEW, "approved, then the index failed")
    assert raised.value.code == ServiceErrorCode.INTERNAL
    assert "makna index sync" in raised.value.message
    for forbidden in ("boom", "Traceback", str(world.repo.path)):
        assert forbidden not in raised.value.message
    head = sup.head(world)
    assert world.repo.show(head, NEW_PATH) is not None  # git is ahead of the index
    assert sup.object_row(world, NEW)["status"] == "in_review"
    open_rows = [r for r in sup.audit_rows(world.engine) if sup.is_open(r)]
    assert [(r["action"], r["object_id"]) for r in open_rows] == [("approve", NEW)]
    assert open_rows[0]["files"] == {
        NEW_PATH: sha256(world.repo.show(head, NEW_PATH) or b""),
        NEW_DRAFT: None,
    }

    assert world.life.sync() is not None
    assert sup.object_row(world, NEW)["status"] == "approved"
    rows = [r for r in sup.audit_rows(world.engine) if r["action"] == "approve"]
    assert [r["commit_sha"] for r in rows] == [head]  # the audit event matches HEAD
    assert not [r for r in sup.audit_rows(world.engine) if sup.is_open(r)]
    with world.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT value FROM index_state WHERE key = 'last_indexed_commit'")
            ).scalar_one()
            == head
        )
    once = sup.state_dump(world.engine)
    world.life.sync()
    assert sup.state_dump(world.engine) == once  # a second sync changes nothing


def _fail(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("boom")


def test_recovery_does_not_trust_an_intent_whose_hashes_do_not_match_the_commit(
    world: World,
) -> None:
    # An intent for an approval of the new metric, and a raw commit that writes other content
    # to the same file: the hashes differ, so the commit is an outside change and the intent is
    # dropped as an action that never happened.
    propose_and_submit(world)
    forged = sup.metric_text(status="approved", version=1, label="Not what was approved")
    (world.repo.path / NEW_PATH).write_text(forged)
    sup.git(world.repo, "add", NEW_PATH)
    sup.git(world.repo, "rm", "-q", "--cached", NEW_DRAFT)
    sup.git(world.repo, "commit", "-q", "-m", "approve(metric:contract_count_total): trust me")
    with world.engine.begin() as connection:
        connection.execute(
            insert(audit_event).values(
                actor_user_id=world.ids["approver"],
                actor_label="approver@example.com",
                action="approve",
                object_id=NEW,
                reason="r",
                files={NEW_PATH: sha256(b"something else"), NEW_DRAFT: None},
            )
        )
    world.life.sync()
    assert not [r for r in sup.audit_rows(world.engine) if sup.is_open(r)]
    approvals = [
        r for r in sup.audit_rows(world.engine) if r["action"] == "approve" and r["commit_sha"]
    ]
    assert approvals == []
    assert sup.object_row(world, NEW)["status"] != "approved"
    externals = [r for r in sup.audit_rows(world.engine) if r["action"] == "external_change"]
    assert [r["object_id"] for r in externals] == [NEW]


def test_an_intent_by_another_author_does_not_complete_with_a_matching_commit(world: World) -> None:
    # Same files and hashes, but the commit was made by someone else than the intent's actor.
    propose_and_submit(world)
    content = sup.edited(
        (world.repo.show(sup.head(world), NEW_DRAFT) or b"").decode(),
        lambda d: d.update(status="approved", version=1),
    )
    (world.repo.path / NEW_PATH).write_text(content)
    sup.git(world.repo, "add", NEW_PATH)
    sup.git(world.repo, "rm", "-q", "--cached", NEW_DRAFT)
    sup.git(world.repo, "commit", "-q", "-m", "x", env={"GIT_AUTHOR_EMAIL": "mallory@example.com"})
    with world.engine.begin() as connection:
        connection.execute(
            insert(audit_event).values(
                actor_user_id=world.ids["approver"],
                actor_label="approver@example.com",
                action="approve",
                object_id=NEW,
                files={NEW_PATH: sha256(content.encode()), NEW_DRAFT: None},
            )
        )
    world.life.sync()
    assert sup.object_row(world, NEW)["status"] != "approved"


# --- criterion 10 ----------------------------------------------------------------------------


def test_index_build_reproduces_the_incremental_index_including_an_outside_change(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.catalog.file import FileConnector
    from makna.catalog.sync import sync_catalog
    from makna.cli.main import main
    from tests.catalog_support import EXPORT, TAG_MAP, sample_objects

    propose_and_submit(world)
    world.life.approve(world.caller("approver"), NEW, "ok")
    text_ = sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="By Hand"))
    path = CHANGED_PATH
    (world.repo.path / path).write_text(text_)  # an outside commit on an approved file
    sup.git(world.repo, "add", path)
    sup.git(world.repo, "commit", "-q", "-m", "hand edit")
    world.life.sync()

    def snapshot() -> dict[str, Any]:
        dump = sup.state_dump(world.engine)
        with world.engine.connect() as connection:  # without updated_at, which is a timestamp
            dump["semantic_object"] = [
                tuple(r)
                for r in connection.execute(
                    text("SELECT to_jsonb(t) - 'updated_at' FROM semantic_object t ORDER BY id")
                )
            ]
        return {
            k: v
            for k, v in dump.items()
            if k in ("semantic_object", "object_edge", "search_document")
        }

    incremental = snapshot()
    with world.engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE semantic_object, search_document, object_edge, reference_issue, "
                "index_state, catalog_glossary_term, catalog_column, catalog_dataset, "
                "sync_run, catalog_source RESTART IDENTITY CASCADE"
            )
        )
    entities, terms = sample_objects()
    sync_catalog(
        world.engine,
        FileConnector(EXPORT),
        tag_map=TAG_MAP,
        entities=entities,
        glossary_terms=terms,
    )
    monkeypatch.setenv("MAKNA_DATABASE_URL", world.url.render_as_string(hide_password=False))
    monkeypatch.setenv("MAKNA_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("MAKNA_DEFINITIONS_REPO", str(world.repo.path))
    assert main(["index", "build"]) == 0
    assert snapshot() == incremental
    row = sup.object_row(world, CHANGED)
    assert row["proposal_origin"] == "external:outside@example.com"
    assert row["proposal_status"] == "in_review" and row["version"] == 1
    assert main(["index", "sync"]) == 0
    assert snapshot() == incremental


# --- the rulings about git and files -------------------------------------------------------


def test_a_path_inside_another_repository_is_a_plain_directory_and_read_only(
    empty_database: URL, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from makna.cli.main import main

    outer = tmp_path / "source"
    inner = outer / "semantic" / "definitions"
    shutil.copytree(DEFINITIONS, inner)
    from makna.definitions.repo import GitRepo

    GitRepo(outer).init()
    world = sup.build_world(empty_database, inner, seed=False)
    try:
        assert not world.life.writable()
        audit = sup.audit_rows(world.engine)
        calls: tuple[Callable[[], Any], ...] = (
            lambda: world.life.create_proposal(
                world.caller("curator"), NEW, sup.metric_text(), "r"
            ),
            lambda: world.life.approve(world.caller("approver"), CHANGED, "r"),
            lambda: world.life.deprecate(world.caller("approver"), CHANGED, "r"),
        )
        for call in calls:
            with pytest.raises(ServiceError) as raised:
                call()
            assert "plain directory" in raised.value.message
            assert raised.value.details == {"read_only": True}
        assert sup.audit_rows(world.engine) == audit == []
        assert world.life.sync() is None
        assert not (inner / "lending/metrics/contract_count_total.draft.yaml").exists()
        # the existing commands behave as before on it: files are served as they are
        os.environ["MAKNA_DATABASE_URL"] = world.url.render_as_string(hide_password=False)
        os.environ["MAKNA_EMBEDDING_PROVIDER"] = "hash"
        try:
            assert main(["validate", str(inner)]) == 0
            assert main(["index", "build", "--definitions", str(inner)]) == 0
            assert main(["index", "sync", "--definitions", str(inner)]) == 2
        finally:
            os.environ.pop("MAKNA_DATABASE_URL", None)
            os.environ.pop("MAKNA_EMBEDDING_PROVIDER", None)
        assert "plain directory" in capsys.readouterr().err
        assert sup.object_row(world, CHANGED)["status"] == "approved"
        assert sup.object_row(world, CHANGED)["official_commit"] is None
    finally:
        world.services.dispose()


def test_a_planted_symbolic_link_is_never_written_through(world: World, tmp_path: Path) -> None:
    # The commit is built from the checked bytes, not from the working tree, so the link cannot
    # redirect it; the working tree is brought in line afterwards without following the link.
    target = tmp_path / "outside.txt"
    target.write_text("do not touch")
    link = world.repo.path / NEW_DRAFT
    link.symlink_to(target)
    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "write through it")
    assert target.read_text() == "do not touch"
    assert link.is_symlink()  # the planted link is left for a person to look at
    assert b"contract_count_total" in (world.repo.show(sup.head(world), NEW_DRAFT) or b"")
    assert sup.object_row(world, NEW)["status"] == "draft"


def test_a_symbolic_link_in_the_folder_path_is_never_written_through(
    world: World, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    folder = world.repo.path / "lending" / "glossary"
    shutil.rmtree(folder)
    folder.symlink_to(outside)
    world.life.create_proposal(
        world.caller("curator"),
        "glossary_term:total_contracts",
        sup.edited(
            sup.official_text(world, "glossary_term:npf"),
            lambda d: d.update(name="total_contracts", label="Total Contracts"),
        ),
        "write into the linked folder",
    )
    assert list(outside.iterdir()) == []
    shown = world.repo.show(sup.head(world), "lending/glossary/total_contracts.draft.yaml")
    assert shown is not None


def test_a_hook_in_the_repository_does_not_run(world: World, tmp_path: Path) -> None:
    marker = tmp_path / "hook-ran"
    for name in ("pre-commit", "commit-msg", "post-commit", "pre-merge-commit"):
        hook = world.repo.path / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
    propose_and_submit(world)
    world.life.approve(world.caller("approver"), NEW, "ok")
    assert not marker.exists()


def test_a_git_failure_leaves_a_failed_intent_and_its_text_reaches_no_error_or_log(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    lock = world.repo.path / ".git" / "refs" / "heads" / "main.lock"
    lock.write_text("held by someone else")
    head, audit = sup.head(world), sup.audit_rows(world.engine)
    with pytest.raises(ServiceError) as raised:
        world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "will fail")
    error = raised.value
    assert error.code == LifecycleErrorCode.CONFLICT
    shown = str(error) + repr(error.details)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for word in ("main.lock", "fatal", "Unable to create", str(world.repo.path), "contract_count"):
        assert word not in shown and word not in logged
    assert sup.head(world) == head
    rows = sup.audit_rows(world.engine)
    assert rows[: len(audit)] == audit and len(rows) == len(audit) + 1
    attempt = rows[-1]  # the attempt is kept, marked failed (it is a trace)
    assert (attempt["action"], attempt["commit_sha"]) == ("create_proposal", None)
    assert attempt["failed_at"] is not None
    assert not (world.repo.path / NEW_DRAFT).exists()  # nothing was written to the working tree
    assert sup.git(world.repo, "status", "--porcelain") == ""
    lock.unlink()
    assert world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "again")


def test_stored_yaml_is_never_echoed_in_an_error(world: World) -> None:
    secret = "SECRET-DESCRIPTION-" + "x" * 30
    text_ = sup.metric_text(description=secret, measure="contract_snapshot.nope")
    world.life.create_proposal(world.caller("curator"), NEW, text_, "has an error")
    with pytest.raises(ServiceError) as raised:
        world.life.submit(world.caller("curator"), NEW, "try")
    assert secret not in str(raised.value) and secret not in repr(raised.value.details)
