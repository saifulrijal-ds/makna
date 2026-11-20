"""WP-19 (WEB-2), the service half: `Lifecycle.check` and `Lifecycle.editor_source`, and the
`expected_proposal_sha256` of the three actions the editor uses (integration tier, with a
temporary git repository)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
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


# --- fix round 1 -------------------------------------------------------------------------------


def stored_description(world: World, size: int) -> str:
    """A new metric whose canonical draft is exactly `size` bytes (a single long word, which the
    canonical form neither wraps nor quotes)."""
    from makna.definitions.render import parse_text, to_yaml
    from makna.services.lifecycle import _as_proposal

    def canonical(k: int) -> int:
        text = sup.metric_text(description="x" * k)
        definition, _ = parse_text(text)
        assert definition is not None
        return len(to_yaml(_as_proposal(definition, "draft", None)).encode())

    k = 20 + size - canonical(20)  # the description needs at least 20 characters
    assert canonical(k) == size
    return sup.metric_text(description="x" * k)


def test_a_text_under_the_limit_whose_stored_form_is_over_it_is_refused_before_any_commit(
    world: World,
) -> None:
    import yaml

    data = yaml.safe_load(sup.metric_text())
    data["description"] = "PLACEHOLDER"
    base = yaml.safe_dump(data, sort_keys=False)
    n = (MAX_YAML_BYTES - len(base) - 20) // 3
    text = base.replace("PLACEHOLDER", '"' + "x\\n" * n + '"')
    assert len(text.encode()) < MAX_YAML_BYTES  # the posted text is allowed
    before = sup.snapshot(world)
    commits = len(world.repo.log())
    with pytest.raises(ServiceError) as refused:
        world.life.create_proposal(world.caller("curator"), NEW, text, "big")
    assert refused.value.code == ServiceErrorCode.INVALID_INPUT
    assert "larger than 256 KiB" in refused.value.message
    assert len(world.repo.log()) == commits
    assert sup.snapshot(world) == before  # no intent row, no failed row
    # Check says the same, with the rule and no line.
    result = world.life.check(world.caller("curator"), NEW, text)
    assert [(i.rule, i.line) for i in result.issues] == [("V01", None)]
    assert "larger than 256 KiB" in result.issues[0].message


def test_a_proposal_whose_approved_or_deprecated_form_would_be_over_the_limit_is_refused_at_save(
    world: World,
) -> None:
    """A saved proposal can always be submitted and approved: the largest form the file will take
    (the version, a deprecation note) is measured when it is saved and when it is checked."""
    curator = world.caller("curator")
    text = stored_description(world, MAX_YAML_BYTES - 5)  # the draft fits, `version:` does not
    before, commits = sup.snapshot(world), len(world.repo.log())
    with pytest.raises(ServiceError) as refused:
        world.life.create_proposal(curator, NEW, text, "just fits as a draft")
    assert refused.value.code == ServiceErrorCode.INVALID_INPUT
    assert "approved" in refused.value.message and "deprecated" in refused.value.message
    assert len(world.repo.log()) == commits and sup.snapshot(world) == before
    result = world.life.check(curator, NEW, text)
    assert [(i.rule, i.line) for i in result.issues] == [("V01", None)]
    assert "approved" in result.issues[0].message
    # A text with room for the version and a long deprecation note is saved, submitted, approved,
    # and deprecated.
    roomy = stored_description(world, MAX_YAML_BYTES - 4000)
    saved = world.life.create_proposal(curator, NEW, roomy, "roomy")
    world.life.submit(curator, NEW, "ready", saved.proposal_sha256)
    world.life.approve(world.caller("approver"), NEW, "ok")
    world.life.deprecate(world.caller("approver"), NEW, "r" * 500)


def test_an_official_file_too_close_to_the_limit_gets_a_message_that_fits_deprecate_and_approve(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services import lifecycle

    retired = "metric:penalty_income"
    size = len(sup.official_text(world, retired).encode())
    monkeypatch.setattr(lifecycle, "MAX_YAML_BYTES", size + 10)
    before = sup.snapshot(world)
    with pytest.raises(ServiceError) as refused:
        world.life.deprecate(world.caller("approver"), retired, "no longer used")
    message = refused.value.message
    assert "deprecating adds a note" in message and "through a proposal" in message
    assert "shorten it" not in message.replace("shorten the definition", "")
    assert sup.snapshot(world) == before


def test_a_refused_text_does_not_take_the_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parse and the identity check come before the advisory lock, so a refused text cannot
    make other writers wait."""
    from makna.services import store

    def no_lock(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the lock was taken")

    monkeypatch.setattr(store, "advisory_lock", no_lock)
    curator = world.caller("curator")
    for text in ("name: [unclosed\n", sup.metric_text(name="other_name"), "a: " + "9" * 5000):
        with pytest.raises(ServiceError) as refused:
            world.life.create_proposal(curator, NEW, text, "r")
        assert refused.value.code in (
            LifecycleErrorCode.DEFINITION_INVALID,
            ServiceErrorCode.INVALID_INPUT,
        )


def test_the_expected_value_is_checked_before_anything_else_under_the_lock(world: World) -> None:
    curator = world.caller("curator")
    with pytest.raises(ServiceError) as refused:
        world.life.create_proposal(
            curator, CHANGED, sup.official_text(world, CHANGED), "r", NEW_OBJECT
        )
    assert refused.value.details == {"exists": True}


def amend_head(world: World) -> None:
    sup.git(world.repo, "commit", "--amend", "-m", "rewritten history", "--allow-empty")


def test_a_rewritten_history_is_a_conflict_with_the_stores_message_in_both_reads(
    world: World,
) -> None:
    amend_head(world)
    curator = world.caller("curator")
    calls: list[Callable[[], Any]] = [
        lambda: world.life.check(curator, NEW, sup.metric_text()),
        lambda: world.life.editor_source(curator, CHANGED),
    ]
    for call in calls:
        with pytest.raises(ServiceError) as refused:
            call()
        assert refused.value.code == LifecycleErrorCode.CONFLICT
        assert refused.value.details.get("fatal") is True
        assert "history" in refused.value.message


def test_an_outside_file_with_a_huge_integer_does_not_stop_sync_or_the_write_path(
    world: World,
) -> None:
    path = world.repo.path / "lending" / "metrics" / "bigint_metric.draft.yaml"
    path.write_text(
        "apiVersion: makna/v1\nkind: metric\nname: bigint_metric\nlabel: " + "9" * 5000 + "\n"
    )
    sup.git(world.repo, "add", "-A")
    sup.git(world.repo, "commit", "-m", "outside file")
    synced = world.life.sync()  # must not raise
    assert synced is not None
    state, _ = synced
    item = state.objects["metric:bigint_metric"]
    assert item.proposal is None and {i.code for i in item.unreadable} == {"V01"}
    assert "too many digits" in item.unreadable[0].message
    # The write path still works for everything else.
    curator = world.caller("curator")
    world.life.create_proposal(curator, NEW, sup.metric_text(), "still works")
    source = world.life.editor_source(world.caller("admin"), "metric:bigint_metric")
    # The text is readable, so it is loaded as it is (an admin may replace it), with no status.
    assert source is not None and source.status is None and source.text.startswith("apiVersion")


def test_an_unreadable_proposal_is_blocked_and_never_an_empty_box_with_a_shared_hash(
    world: World,
) -> None:
    def outside(name: str, filler: str) -> None:
        path = world.repo.path / "lending" / "metrics" / f"{name}.draft.yaml"
        path.write_text("# " + filler * (MAX_YAML_BYTES // len(filler) + 10) + "\n")
        sup.git(world.repo, "add", "-A")
        sup.git(world.repo, "commit", "-m", f"outside {name}")

    outside("huge_one", "a")
    outside("huge_two", "b")
    admin = world.caller("admin")
    one = world.life.editor_source(admin, "metric:huge_one")
    two = world.life.editor_source(admin, "metric:huge_two")
    assert one is not None and two is not None
    for source in (one, two):
        assert source.text == "" and source.blocked and "cannot be read" in source.blocked
        assert source.proposal_sha256 != sha256(b"")
    assert one.proposal_sha256 != two.proposal_sha256


# --- fix round 2 -------------------------------------------------------------------------------


def make_proposals(world: World, who: str, count: int, start: int = 0) -> list[str]:
    """`count` new draft metrics by `who`, named by number."""
    ids = []
    for n in range(start, start + count):
        name = f"extra_metric_{n}"
        world.life.create_proposal(
            world.caller(who), f"metric:{name}", sup.metric_text(name=name), f"draft {n}"
        )
        ids.append(f"metric:{name}")
    return ids


def test_a_user_may_have_only_so_many_open_proposals(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services import lifecycle

    monkeypatch.setattr(lifecycle, "MAX_OPEN_DRAFTS_PER_USER", 3)
    curator = world.caller("curator")
    ids = make_proposals(world, "curator", 3)  # exactly at the limit: all three are made
    commits = len(world.repo.log())
    with pytest.raises(ServiceError) as over:  # one over
        make_proposals(world, "curator", 1, start=3)
    assert over.value.code == LifecycleErrorCode.TOO_MANY_DRAFTS
    assert over.value.details == {"open": 3, "maximum": 3} and "3" in over.value.message
    assert len(world.repo.log()) == commits
    # Updating or submitting what is open is never blocked; an in-review proposal still counts.
    sha = world.life.editor_source(curator, ids[0]).proposal_sha256
    world.life.update_proposal(
        curator, ids[0], sup.metric_text(name="extra_metric_0", label="Edited"), "edit", sha
    )
    world.life.submit(curator, ids[1], "ready")
    with pytest.raises(ServiceError) as still:
        make_proposals(world, "curator", 1, start=3)
    assert still.value.code == LifecycleErrorCode.TOO_MANY_DRAFTS
    # Another user is not affected, and a change of an official object counts like any other.
    make_proposals(world, "curator2", 1, start=10)
    # After a withdraw there is room again; after an approval too.
    world.life.withdraw(curator, ids[2], "not needed")
    make_proposals(world, "curator", 1, start=3)
    with pytest.raises(ServiceError):
        make_proposals(world, "curator", 1, start=4)
    world.life.approve(world.caller("approver"), ids[1], "ok")
    make_proposals(world, "curator", 1, start=4)


def test_a_proposal_from_an_outside_commit_counts_for_nobody(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services import lifecycle

    monkeypatch.setattr(lifecycle, "MAX_OPEN_DRAFTS_PER_USER", 1)
    path = world.repo.path / "lending" / "metrics" / "outside_metric.draft.yaml"
    path.write_text(sup.metric_text(name="outside_metric", status="draft"))
    sup.git(world.repo, "add", "-A")
    sup.git(world.repo, "commit", "-m", "an outside draft")
    make_proposals(world, "curator", 1)  # the outside draft is nobody's
    with pytest.raises(ServiceError) as over:
        make_proposals(world, "curator", 1, start=1)
    assert over.value.code == LifecycleErrorCode.TOO_MANY_DRAFTS


def test_the_limit_is_twenty() -> None:
    from makna.services.lifecycle import MAX_OPEN_DRAFTS_PER_USER

    assert MAX_OPEN_DRAFTS_PER_USER == 20


def test_an_unreadable_proposal_cannot_be_replaced_or_submitted_but_can_be_withdrawn(
    world: World,
) -> None:
    path = world.repo.path / "lending" / "metrics" / "huge_one.draft.yaml"
    path.write_text("# " + "a" * (MAX_YAML_BYTES + 10) + "\n")
    sup.git(world.repo, "add", "-A")
    sup.git(world.repo, "commit", "-m", "outside huge file")
    admin = world.caller("admin")
    source = world.life.editor_source(admin, "metric:huge_one")
    assert source is not None and source.can_withdraw and source.blocked
    commits = len(world.repo.log())
    calls: list[Callable[[], Any]] = [
        lambda: world.life.update_proposal(
            admin, "metric:huge_one", sup.metric_text(name="huge_one"), "r", source.proposal_sha256
        ),
        lambda: world.life.update_proposal(
            admin, "metric:huge_one", sup.metric_text(name="huge_one"), "r"
        ),
        lambda: world.life.submit(admin, "metric:huge_one", "r"),
        lambda: world.life.submit(admin, "metric:huge_one", "r", source.proposal_sha256),
    ]
    for call in calls:
        with pytest.raises(ServiceError) as refused:
            call()
        assert refused.value.code == LifecycleErrorCode.CONFLICT
        assert "cannot be read here" in refused.value.message
    assert len(world.repo.log()) == commits
    done = world.life.withdraw(admin, "metric:huge_one", "unreadable")
    assert done.status == "withdrawn"
    assert world.life.editor_source(admin, "metric:huge_one") is None


def test_who_may_withdraw_is_the_lifecycles_rule_and_the_editor_source_says_so(
    world: World,
) -> None:
    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "mine")
    assert world.life.editor_source(world.caller("curator"), NEW).can_withdraw
    assert world.life.editor_source(world.caller("approver"), NEW).can_withdraw
    other = world.life.editor_source(world.caller("outsider"), NEW)
    assert other is not None and not other.can_withdraw
    official = world.life.editor_source(world.caller("curator"), CHANGED)
    assert official is not None and not official.can_withdraw  # no proposal, nothing to withdraw


def test_a_refused_create_with_a_wrong_expectation_stops_before_the_domain_and_the_rest(
    world: World,
) -> None:
    """`_expect` is the first check inside the lock-time plan: a text whose domain does not exist
    still gets the "already exists" answer for an object that exists, not a domain error."""
    text = sup.edited(
        sup.official_text(world, CHANGED), lambda d: d.update(domain="no_such_domain")
    )
    with pytest.raises(ServiceError) as refused:
        world.life.create_proposal(world.caller("curator"), CHANGED, text, "r", NEW_OBJECT)
    assert refused.value.code == LifecycleErrorCode.CONFLICT
    assert refused.value.details == {"exists": True}


INTEGERS = {
    "decimal": "9" * 5000,
    "hex": "0x" + "f" * 4000,
    "octal": "0" + "7" * 5000,
    "binary": "0b" + "1" * 20000,
    "negative hex": "-0x" + "f" * 4000,
}


def metric_with_filter_value(value: str) -> str:
    return (
        sup.metric_text()
        + f"filters:\n  - {{dimension: contract_snapshot.branch, op: '=', value: {value}}}\n"
    )


@pytest.mark.parametrize("kind", list(INTEGERS))
def test_an_integer_over_the_limit_in_any_base_is_a_v01_issue_with_a_line(
    world: World, kind: str
) -> None:
    value = INTEGERS[kind]
    text = metric_with_filter_value(value)
    line = text.splitlines().index(next(r for r in text.splitlines() if value in r)) + 1
    curator = world.caller("curator")
    result = world.life.check(curator, NEW, text)
    assert [(i.rule, i.line) for i in result.issues][:1] == [("V01", line)]
    with pytest.raises(ServiceError) as refused:
        world.life.create_proposal(curator, NEW, text, "r")
    assert refused.value.code == LifecycleErrorCode.DEFINITION_INVALID
    # The same number in an outside file: sync completes, reports the file, the write path works.
    path = world.repo.path / "lending" / "metrics" / "outside_number.draft.yaml"
    path.write_text(
        metric_with_filter_value(value).replace("contract_count_total", "outside_number")
    )
    sup.git(world.repo, "add", "-A")
    sup.git(world.repo, "commit", "-m", "an outside file")
    synced = world.life.sync()
    assert synced is not None
    item = synced[0].objects["metric:outside_number"]
    assert item.proposal is None and {i.code for i in item.unreadable} == {"V01"}
    world.life.create_proposal(curator, NEW, sup.metric_text(), "still works")


def test_the_last_line_of_defence_for_the_stored_size_words_each_action() -> None:
    """`_check_stored_size` is what stops an action whose written file is over the limit, whatever
    the proposal looked like when it was saved; each action gets a message that fits it."""
    from makna.services import lifecycle

    plan = lifecycle._Plan({"a/b.yaml": b"x" * (MAX_YAML_BYTES + 1)}, "approved", "deprecated", 1)
    messages = {}
    for action in (
        lifecycle.DEPRECATE,
        lifecycle.APPROVE,
        lifecycle.SUBMIT,
        lifecycle.CREATE_PROPOSAL,
    ):
        with pytest.raises(ServiceError) as refused:
            lifecycle._check_stored_size(plan, action)
        messages[action] = refused.value.message
    assert "deprecating adds a note" in messages[lifecycle.DEPRECATE]
    assert "through a proposal" in messages[lifecycle.DEPRECATE]
    assert "approving adds the version" in messages[lifecycle.APPROVE]
    assert "stored form" in messages[lifecycle.SUBMIT]
    lifecycle._check_stored_size(
        lifecycle._Plan({"a/b.yaml": b"x" * MAX_YAML_BYTES}, None, "draft", None), "submit"
    )


def reject_outside_edits(world: World, ids: list[str]) -> None:
    """An outside commit edits each official file; an approver rejects each change, which writes
    the outside content as a draft of the application."""
    for object_id in ids:
        name = object_id.partition(":")[2]
        path = world.repo.path / "lending" / "metrics" / f"{name}.yaml"
        path.write_text(
            path.read_text().replace("label: Total Contract Count", "label: Edited outside")
        )
        sup.git(world.repo, "add", "-A")
        sup.git(world.repo, "commit", "-m", f"outside edit of {name}")
    assert world.life.sync() is not None
    for object_id in ids:
        world.life.reject(world.caller("approver"), object_id, "outside change rejected")


def test_a_draft_made_by_rejecting_an_outside_change_counts_for_nobody(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap charges a proposal to someone who started it and may withdraw it. The draft a
    reject makes from an outside change has the outside author and the rejecting approver as its
    authors, so it is not charged to the creator of the object (who could not withdraw it)."""
    from makna.services import lifecycle

    monkeypatch.setattr(lifecycle, "MAX_OPEN_DRAFTS_PER_USER", 3)
    curator, approver = world.caller("curator"), world.caller("approver")
    ids = make_proposals(world, "curator", 3)
    for object_id in ids:
        world.life.submit(curator, object_id, "ready")
        world.life.approve(approver, object_id, "ok")
    reject_outside_edits(world, ids)
    for object_id in ids:
        source = world.life.editor_source(curator, object_id)
        assert source is not None and source.loaded == "proposal" and not source.can_withdraw
    make_proposals(world, "curator", 1, start=10)  # not refused: nothing is charged to the creator
    make_proposals(world, "approver", 1, start=11)  # nor to the approver who rejected


def test_a_failed_attempt_does_not_move_a_proposal_to_another_user(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import func, insert

    from makna.index.tables import audit_event
    from makna.services import lifecycle

    monkeypatch.setattr(lifecycle, "MAX_OPEN_DRAFTS_PER_USER", 1)
    ids = make_proposals(world, "curator2", 1)
    with world.engine.begin() as connection:  # an attempt that was given up, by another user
        connection.execute(
            insert(audit_event).values(
                actor_user_id=world.ids["curator"],
                actor_label="curator@example.com",
                action="create_proposal",
                object_id=ids[0],
                to_status="draft",
                reason="given up",
                commit_sha=None,
                files={},
                failed_at=func.now(),
            )
        )
    with pytest.raises(ServiceError) as over:  # the proposal is still curator2's
        make_proposals(world, "curator2", 1, start=5)
    assert over.value.code == LifecycleErrorCode.TOO_MANY_DRAFTS


def test_the_latest_create_decides_who_a_proposal_counts_for(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services import lifecycle

    monkeypatch.setattr(lifecycle, "MAX_OPEN_DRAFTS_PER_USER", 1)
    first = make_proposals(world, "curator", 1)
    world.life.withdraw(world.caller("curator"), first[0], "starting over")
    make_proposals(world, "curator2", 1)  # the same name, made again by someone else
    make_proposals(world, "curator", 1, start=7)  # the first user is free again
    with pytest.raises(ServiceError) as over:
        make_proposals(world, "curator2", 1, start=8)
    assert over.value.code == LifecycleErrorCode.TOO_MANY_DRAFTS


def test_a_withdraw_with_a_stale_fingerprint_keeps_the_newer_draft(world: World) -> None:
    curator, approver = world.caller("curator"), world.caller("approver")
    saved = world.life.create_proposal(curator, NEW, sup.metric_text(), "first")
    shown = saved.proposal_sha256
    world.life.update_proposal(curator, NEW, sup.metric_text(label="A newer draft"), "newer", shown)
    commits = len(world.repo.log())
    with pytest.raises(ServiceError) as stale:
        world.life.withdraw(approver, NEW, "clean up", shown)
    assert stale.value.code == LifecycleErrorCode.CONFLICT and stale.value.details == {
        "changed": True
    }
    assert len(world.repo.log()) == commits
    assert "A newer draft" in (world.life.editor_source(curator, NEW).text)
    # Without a value (other callers) or with the current one it works.
    current = world.life.editor_source(curator, NEW).proposal_sha256
    assert world.life.withdraw(approver, NEW, "clean up", current).status == "withdrawn"


def test_headroom_is_checked_when_a_proposal_is_created_and_when_it_is_updated(
    world: World,
) -> None:
    curator = world.caller("curator")
    too_big = stored_description(world, MAX_YAML_BYTES - 100)  # the draft fits, later forms do not
    with pytest.raises(ServiceError) as on_create:
        world.life.create_proposal(curator, NEW, too_big, "r")
    assert on_create.value.code == ServiceErrorCode.INVALID_INPUT
    saved = world.life.create_proposal(curator, NEW, sup.metric_text(), "small")
    commits = len(world.repo.log())
    with pytest.raises(ServiceError) as on_update:
        world.life.update_proposal(curator, NEW, too_big, "grow", saved.proposal_sha256)
    assert on_update.value.code == ServiceErrorCode.INVALID_INPUT
    assert "approved (with its version)" in on_update.value.message
    assert len(world.repo.log()) == commits


@pytest.mark.parametrize(
    "reason",
    ["\U0001f600" * 500, "'" * 250 + '"' * 250, "\\" * 500, "'\"" * 250, "a: " * 166, "x" * 500],
)
def test_the_largest_allowed_draft_can_still_be_deprecated_with_the_worst_reason(
    world: World, reason: str
) -> None:
    from makna.definitions.render import parse_text
    from makna.services.lifecycle import _later_forms_too_large

    def fits(k: int) -> bool:
        definition, problems = parse_text(sup.metric_text(description="x" * k))
        assert definition is not None, problems
        return not _later_forms_too_large(definition, None)

    low, high = 20, MAX_YAML_BYTES
    while low < high:  # the longest description whose later forms still fit
        middle = (low + high + 1) // 2
        low, high = (middle, high) if fits(middle) else (low, middle - 1)
    assert not fits(low + 1)
    text = sup.metric_text(description="x" * low)
    curator, approver = world.caller("curator"), world.caller("approver")
    saved = world.life.create_proposal(curator, NEW, text, "the largest")
    world.life.submit(curator, NEW, "ready", saved.proposal_sha256)
    world.life.approve(approver, NEW, "ok")
    done = world.life.deprecate(approver, NEW, reason)
    assert done.status == "deprecated"
