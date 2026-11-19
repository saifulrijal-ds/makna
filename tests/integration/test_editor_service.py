"""WP-19 (WEB-2), the service half: `Lifecycle.check` and `Lifecycle.editor_source`, and the
`expected_proposal_sha256` of the three actions the editor uses (integration tier, with a
temporary git repository)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import URL

from makna.definitions.render import MAX_YAML_BYTES, sha256
from makna.services import ServiceError, ServiceErrorCode
from makna.services.editing import NEW_OBJECT, NO_PROPOSAL
from makna.services.errors import LifecycleErrorCode

from . import lifecycle_support as sup
from .lifecycle_support import World

NEW = "metric:contract_count_total"
NEW_DRAFT = "lending/metrics/contract_count_total.draft.yaml"
SNAPSHOT = "entity:contract_snapshot"
CHANGED = "metric:disbursement_amount"


@pytest.fixture
def world(empty_database: URL, tmp_path: Path) -> Iterator[World]:
    built = sup.build_world(empty_database, tmp_path / "definitions")
    yield built
    built.services.dispose()


def rules(result: Any) -> list[tuple[str, int | None]]:
    return [(i.rule, i.line) for i in result.issues]


# --- criterion 1: Check ------------------------------------------------------------------------


def test_check_reports_an_unknown_key_with_its_line_and_writes_nothing(world: World) -> None:
    text = sup.metric_text() + "colour: blue\n"
    before = sup.snapshot(world)
    result = world.life.check(world.caller("curator"), NEW, text)
    line = text.splitlines().index("colour: blue") + 1
    assert not result.ok
    assert [(i.rule, i.line, i.own) for i in result.issues] == [("V01", line, True)]
    assert "colour" in result.issues[0].message
    assert sup.snapshot(world) == before


def test_check_reports_a_v06_break_with_its_line_and_writes_nothing(world: World) -> None:
    text = sup.official_text(world, SNAPSHOT)
    # Point a measure's time dimension at a dimension that is not of type time.
    edited = text.replace("time_dimension: snapshot_date", "time_dimension: dpd_bucket", 1)
    assert edited != text
    before = sup.snapshot(world)
    result = world.life.check(world.caller("curator"), SNAPSHOT, edited)
    found = [i for i in result.issues if i.rule == "V06"]
    assert found, rules(result)
    expected_line = edited.splitlines().index(
        next(line for line in edited.splitlines() if "time_dimension: dpd_bucket" in line)
    )
    assert found[0].line == expected_line + 1
    assert found[0].own
    assert sup.snapshot(world) == before


def test_check_on_a_good_text_finds_nothing_for_a_new_and_an_existing_object(world: World) -> None:
    curator = world.caller("curator")
    new = world.life.check(curator, NEW, sup.metric_text())
    assert new.ok and not new.exists and new.issues == ()
    official = world.life.check(curator, CHANGED, sup.official_text(world, CHANGED))
    assert official.ok and official.exists


def test_check_runs_the_whole_gate_so_a_change_that_breaks_another_object_is_listed(
    world: World,
) -> None:
    # Renaming the `region_name` dimension breaks the metrics and examples that use it.
    text = sup.official_text(world, "entity:branch")
    edited = text.replace("name: region_name", "name: region_label", 1)
    result = world.life.check(world.caller("curator"), "entity:branch", edited)
    others = [i for i in result.issues if not i.own]
    assert others, rules(result)
    assert all(
        i.file.endswith(".yaml") and i.file != "lending/entities/branch.yaml" for i in others
    )
    # Own problems come first, then the other files'.
    flags = [i.own for i in result.issues]
    assert flags == sorted(flags, reverse=True)


def test_check_sorts_by_line_and_refuses_the_wrong_object_and_a_moved_domain(world: World) -> None:
    curator = world.caller("curator")
    text = sup.metric_text(label=None, description="short")
    result = world.life.check(curator, NEW, text)
    lines = [i.line for i in result.issues]
    assert len(lines) >= 2 and lines == sorted(lines)
    other = world.life.check(curator, "metric:somebody_else", sup.metric_text())
    assert [(i.rule, i.own) for i in other.issues] == [("V02", True)]
    assert other.issues[0].line == 3  # the `name` line
    moved = world.life.check(
        curator,
        CHANGED,
        sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(domain="marketing")),
    )
    assert moved.issues and moved.issues[0].rule == "V02"


def test_check_gives_a_fixed_issue_for_a_deeply_nested_text(world: World) -> None:
    deep = "a: " + "[" * 3000 + "]" * 3000 + "\n"
    result = world.life.check(world.caller("curator"), NEW, deep)
    assert [i.rule for i in result.issues] == ["V01"]
    assert "[[[[" not in result.issues[0].message


def test_check_needs_the_edit_role_and_the_size_limit(world: World) -> None:
    for who in (world.caller("viewer"), world.token("curator"), world.token("admin")):
        with pytest.raises(ServiceError) as denied:
            world.life.check(who, NEW, sup.metric_text())
        assert denied.value.code == ServiceErrorCode.PERMISSION_DENIED
    big = "x: " + "a" * MAX_YAML_BYTES
    with pytest.raises(ServiceError) as refused:
        world.life.check(world.caller("curator"), NEW, big)
    assert refused.value.code == ServiceErrorCode.INVALID_INPUT
    with pytest.raises(ServiceError) as bad_id:
        world.life.check(world.caller("curator"), "metric:../x", sup.metric_text())
    assert bad_id.value.code == ServiceErrorCode.INVALID_INPUT


def test_check_in_a_plain_directory_says_it_is_read_only(
    empty_database: URL, tmp_path: Path
) -> None:
    from makna.index import migrate

    plain = sup.build_world(empty_database, tmp_path / "definitions")
    plain.services.dispose()
    # A directory without its own repository (inside none) is a configured path that is an error
    # or read-only; the sample directory inside the source tree is the plain-directory case.
    from makna.model.governance import STRICT
    from makna.search.embedding import HashProvider
    from makna.services import Services
    from tests.catalog_support import DEFINITIONS

    migrate.upgrade(empty_database.render_as_string(hide_password=False))
    services = Services(
        plain.engine, HashProvider(), policy=STRICT, definitions_repo=str(DEFINITIONS)
    )
    try:
        with pytest.raises(ServiceError) as refused:
            services.lifecycle.check(plain.caller("curator"), NEW, sup.metric_text())
        assert refused.value.code == LifecycleErrorCode.CONFLICT
        assert refused.value.details.get("read_only") is True
        with pytest.raises(ServiceError) as source:
            services.lifecycle.editor_source(plain.caller("curator"), CHANGED)
        assert source.value.details.get("read_only") is True
    finally:
        services.dispose()


# --- editor_source -----------------------------------------------------------------------------


def test_editor_source_loads_the_official_text_then_the_proposal(world: World) -> None:
    curator = world.caller("curator")
    assert world.life.editor_source(curator, "metric:no_such_thing") is None
    official = world.life.editor_source(curator, CHANGED)
    assert official is not None
    assert (official.loaded, official.status, official.version) == ("official", "approved", 1)
    assert official.proposal_sha256 == NO_PROPOSAL and official.blocked is None
    assert official.official_text is not None and "status: draft" in official.official_text
    assert "name: disbursement_amount" in official.text
    changed = sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="Disbursed"))
    saved = world.life.create_proposal(curator, CHANGED, changed, "a new label")
    proposal = world.life.editor_source(curator, CHANGED)
    assert proposal is not None
    assert (proposal.loaded, proposal.status) == ("proposal", "draft")
    assert "label: Disbursed" in proposal.text
    assert proposal.proposal_sha256 == saved.proposal_sha256
    assert proposal.proposal_sha256 == sha256(proposal.text.encode())
    assert proposal.official_text == official.official_text


def test_editor_source_of_a_new_proposal_has_no_official_text(world: World) -> None:
    curator = world.caller("curator")
    world.life.create_proposal(curator, NEW, sup.metric_text(), "first")
    source = world.life.editor_source(curator, NEW)
    assert source is not None and source.official_text is None and source.version is None
    assert source.loaded == "proposal"


def test_editor_source_says_why_the_caller_cannot_save(world: World) -> None:
    curator = world.caller("curator")
    world.life.create_proposal(curator, NEW, sup.metric_text(), "first")
    other = world.life.editor_source(world.caller("outsider"), NEW)
    assert other is not None and other.blocked and "author" in other.blocked
    assert world.life.editor_source(world.caller("approver"), NEW).blocked is None
    # An approved object that is then deprecated cannot be changed.
    world.life.submit(curator, NEW, "ready")
    world.life.approve(world.caller("approver"), NEW, "ok")
    world.life.deprecate(world.caller("approver"), NEW, "not needed")
    source = world.life.editor_source(curator, NEW)
    assert source is not None and source.status == "deprecated"
    assert source.blocked == "A deprecated object cannot be changed."


def test_editor_source_needs_the_edit_role(world: World) -> None:
    for who in (world.caller("viewer"), world.token("admin")):
        with pytest.raises(ServiceError) as denied:
            world.life.editor_source(who, CHANGED)
        assert denied.value.code == ServiceErrorCode.PERMISSION_DENIED


# --- the lost-update check ---------------------------------------------------------------------


def test_an_action_returns_the_sha_of_the_proposal_file_it_wrote(world: World) -> None:
    curator = world.caller("curator")
    saved = world.life.create_proposal(curator, NEW, sup.metric_text(), "first")
    stored = world.repo.show(sup.head(world), NEW_DRAFT)
    assert stored is not None and saved.proposal_sha256 == sha256(stored)
    submitted = world.life.submit(curator, NEW, "ready", saved.proposal_sha256)
    stored = world.repo.show(sup.head(world), NEW_DRAFT)
    assert stored is not None and submitted.proposal_sha256 == sha256(stored)
    approved = world.life.approve(world.caller("approver"), NEW, "ok")
    assert approved.proposal_sha256 is None


def test_a_second_editor_with_a_stale_hash_gets_a_conflict_and_nothing_is_overwritten(
    world: World,
) -> None:
    curator, curator2 = world.caller("curator"), world.caller("approver")
    first = world.life.create_proposal(
        curator, NEW, sup.metric_text(), "first", expected_proposal_sha256=NEW_OBJECT
    )
    loaded_by_both = first.proposal_sha256
    mine = sup.metric_text(label="Contracts, mine")
    theirs = sup.metric_text(label="Contracts, theirs")
    world.life.update_proposal(curator2, NEW, theirs, "their edit", loaded_by_both)
    commits = len(world.repo.log())
    audit = len(sup.audit_rows(world.engine))
    with pytest.raises(ServiceError) as conflict:
        world.life.update_proposal(curator, NEW, mine, "my edit", loaded_by_both)
    assert conflict.value.code == LifecycleErrorCode.CONFLICT
    assert conflict.value.details == {"changed": True}
    assert (len(world.repo.log()), len(sup.audit_rows(world.engine))) == (commits, audit)
    stored = (world.repo.show(sup.head(world), NEW_DRAFT) or b"").decode()
    assert "Contracts, theirs" in stored and "Contracts, mine" not in stored
    # A submit that expects the old file is refused too.
    with pytest.raises(ServiceError) as late:
        world.life.submit(curator, NEW, "ready", loaded_by_both)
    assert late.value.code == LifecycleErrorCode.CONFLICT


def test_expectations_for_none_and_new(world: World) -> None:
    curator = world.caller("curator")
    # The object exists: a "new" object is refused and nothing is written.
    commits = len(world.repo.log())
    with pytest.raises(ServiceError) as exists:
        world.life.create_proposal(
            curator,
            CHANGED,
            sup.official_text(world, CHANGED),
            "x",
            expected_proposal_sha256=NEW_OBJECT,
        )
    assert exists.value.details == {"exists": True}
    assert len(world.repo.log()) == commits
    # "none" lets a change of an existing object through, and only while it has no proposal.
    text = sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="Disbursed"))
    world.life.create_proposal(curator, CHANGED, text, "x", expected_proposal_sha256=NO_PROPOSAL)
    with pytest.raises(ServiceError) as changed:
        world.life.create_proposal(
            curator, CHANGED, text, "y", expected_proposal_sha256=NO_PROPOSAL
        )
    assert changed.value.details == {"changed": True}
    # An update that expects "none" is refused: there is a proposal now.
    with pytest.raises(ServiceError) as stale:
        world.life.update_proposal(curator, CHANGED, text, "z", NO_PROPOSAL)
    assert stale.value.code == LifecycleErrorCode.CONFLICT


def test_an_unchanged_text_is_a_conflict_that_says_so(world: World) -> None:
    curator = world.caller("curator")
    saved = world.life.create_proposal(curator, NEW, sup.metric_text(), "first")
    with pytest.raises(ServiceError) as same:
        world.life.update_proposal(curator, NEW, sup.metric_text(), "again", saved.proposal_sha256)
    assert same.value.code == LifecycleErrorCode.CONFLICT
    assert same.value.details == {"unchanged": True}


def test_the_expectation_must_look_like_one(world: World) -> None:
    for bad in ("", "x", "A" * 64, "g" * 64, "0" * 63, 5):
        with pytest.raises(ServiceError) as refused:
            world.life.create_proposal(
                world.caller("curator"),
                NEW,
                sup.metric_text(),
                "r",
                bad,
            )
        assert refused.value.code == ServiceErrorCode.INVALID_INPUT, bad


def test_existing_callers_work_without_the_parameter(world: World) -> None:
    curator = world.caller("curator")
    world.life.create_proposal(curator, NEW, sup.metric_text(), "first")
    world.life.update_proposal(curator, NEW, sup.metric_text(label="Contracts"), "second")
    world.life.submit(curator, NEW, "ready")
