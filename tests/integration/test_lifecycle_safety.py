"""WP-18 after the safety and task reviews: one test per reproducer.

C-1 a broken repository is never read as plain files; I-1 a rewritten history is refused;
I-2 and I-3 a commit is built from the checked bytes, not from the working tree; I-4 D-20 sees
renames and removals; I-5 a file's identity is its path; and the minor findings and the
test gaps of the task review.
"""

from __future__ import annotations

import os
import shutil
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import URL

from deploy import init
from deploy.init import Context
from makna.cli.main import main
from makna.definitions.render import sha256
from makna.definitions.repo import GitRepo
from makna.index import indexer
from makna.index.tables import app_user
from makna.services import ServiceError, ServiceErrorCode, store
from makna.services.errors import LifecycleErrorCode
from makna.services.lifecycle import Lifecycle, seed_repository
from tests.catalog_support import DEFINITIONS, EXPORT

from . import lifecycle_support as sup
from .lifecycle_support import World

NEW = "metric:contract_count_total"
NEW_PATH = "lending/metrics/contract_count_total.yaml"
NEW_DRAFT = "lending/metrics/contract_count_total.draft.yaml"
CHANGED = "metric:disbursement_amount"
CHANGED_PATH = "lending/metrics/disbursement_amount.yaml"
CUSTOMER = "entity:customer"
NPF = "metric:npf_ratio"
NPF_PATH = "lending/metrics/npf_ratio.yaml"


@pytest.fixture
def world(empty_database: URL, tmp_path: Path) -> Iterator[World]:
    built = sup.build_world(empty_database, tmp_path / "definitions")
    yield built
    built.services.dispose()


def approve_new(world: World) -> None:
    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "draft")
    world.life.submit(world.caller("curator"), NEW, "ready")
    world.life.approve(world.caller("approver"), NEW, "ok")


def raw_commit(world: World, files: dict[str, str], message: str = "outside", **env: str) -> None:
    for name, content in files.items():
        target = world.repo.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        sup.git(world.repo, "add", "--", name)
    sup.git(world.repo, "commit", "-q", "-m", message, env=env)


def official_files(world: World) -> dict[str, bytes]:
    head = sup.head(world)
    return {p: world.repo.show(head, p) or b"" for p in world.repo.regular_files(head)}


def use_environment(world: World, monkeypatch: pytest.MonkeyPatch, *, repo: bool = True) -> None:
    monkeypatch.setenv("MAKNA_DATABASE_URL", world.url.render_as_string(hide_password=False))
    monkeypatch.setenv("MAKNA_EMBEDDING_PROVIDER", "hash")
    if repo:
        monkeypatch.setenv("MAKNA_DEFINITIONS_REPO", str(world.repo.path))


# --- C-1 -------------------------------------------------------------------------------------


def break_repository(world: World, how: str, scratch: Path) -> None:
    git_dir = world.repo.path / ".git"
    if how == "bare":
        sup.git(world.repo, "config", "core.bare", "true")
    elif how == "worktree":
        sup.git(world.repo, "config", "core.worktree", str(scratch))
    elif how == "rmgit":
        shutil.rmtree(git_dir)
    elif how == "symlink":
        shutil.move(str(git_dir), str(scratch / "moved-git"))
        git_dir.symlink_to(scratch / "moved-git")
    elif how == "gitfile":
        shutil.move(str(git_dir), str(scratch / "moved-git"))
        git_dir.write_text(f"gitdir: {scratch / 'moved-git'}\n")
    else:  # pragma: no cover
        raise AssertionError(how)


@pytest.mark.parametrize("how", ["bare", "worktree", "rmgit", "symlink", "gitfile"])
def test_a_broken_repository_is_an_error_and_never_plain_files(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    how: str,
) -> None:
    approve_new(world)
    # Files nobody committed: an approved new metric, and an edit of an official file.
    (world.repo.path / "lending/metrics/sneaky.yaml").write_text(
        sup.metric_text(name="sneaky", status="approved", version=1)
    )
    edit = sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="Edited"))
    (world.repo.path / CHANGED_PATH).write_text(edit)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    break_repository(world, how, scratch)
    before = sup.state_dump(world.engine)
    use_environment(world, monkeypatch)

    assert main(["index", "build"]) == 2
    assert main(["index", "sync"]) == 2
    assert main(["catalog", "sync", "--file", str(EXPORT)]) == 2
    error = capsys.readouterr().err
    assert "definitions" in error.lower() or "repository" in error.lower()
    assert str(world.repo.path) not in error
    assert sup.state_dump(world.engine) == before  # the index is as it was
    assert not sup.has_object(world, "metric:sneaky")
    assert sup.object_row(world, NEW)["status"] == "approved"

    context = Context(
        data_dir=tmp_path,
        seed=1,
        scale=0.05,
        database_url=world.url.render_as_string(hide_password=False),
        lending_loader_url="postgresql://unused",
        definitions_dir=DEFINITIONS,
        definitions_repo=world.repo.path,
    )
    for step in (init.sync_catalog_step, init.build_index_step):
        with pytest.raises(init.InitError):
            step(context)
    assert sup.state_dump(world.engine) == before
    with pytest.raises(ServiceError) as raised:  # the application's own start-up sync
        world.life.sync()
    assert raised.value.details["unusable"]
    with pytest.raises(ServiceError):
        world.life.create_proposal(
            world.caller("curator"), "metric:other_one", sup.metric_text(name="other_one"), "r"
        )


def test_a_directory_that_was_given_on_the_command_line_is_still_read_as_plain_files(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without MAKNA_DEFINITIONS_REPO the plain reading of the given directory is unchanged.
    use_environment(world, monkeypatch, repo=False)
    monkeypatch.delenv("MAKNA_DEFINITIONS_REPO", raising=False)
    plain = tmp_path / "plain"
    shutil.copytree(DEFINITIONS, plain)
    assert main(["index", "build", "--definitions", str(plain)]) == 0


# --- I-1 -------------------------------------------------------------------------------------


def test_a_reset_of_the_history_is_refused_and_nothing_is_rolled_back(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    approve_new(world)
    world.life.deprecate(world.caller("approver"), "metric:par30_ratio", "retired")
    seed = world.repo.log()[0].sha
    sup.git(world.repo, "reset", "-q", "--hard", seed)
    before = sup.state_dump(world.engine)
    with pytest.raises(ServiceError) as raised:
        world.life.sync()
    assert raised.value.code == LifecycleErrorCode.CONFLICT
    assert raised.value.details["fatal"] is True and "rewritten" in raised.value.message
    assert sup.state_dump(world.engine) == before
    token = world.token("admin")
    assert world.services.get_definition(token, NEW).version == 1  # still served
    assert world.services.get_definition(token, "metric:par30_ratio").status == "deprecated"
    with pytest.raises(ServiceError) as raised:  # no action on a rewritten repository either
        world.life.create_proposal(
            world.caller("curator"), "metric:x_one", sup.metric_text(name="x_one"), "r"
        )
    assert "rewritten" in raised.value.message
    use_environment(world, monkeypatch)
    assert main(["index", "sync"]) == 1
    assert "rewritten" in capsys.readouterr().err
    assert main(["catalog", "sync", "--file", str(EXPORT)]) == 1
    assert sup.state_dump(world.engine) == before
    # The operator decides: this accepts the new history, and the approvals in it are gone.
    assert main(["index", "sync", "--accept-rewritten-history"]) == 0
    assert not sup.has_object(world, NEW)
    assert world.services.get_definition(token, "metric:par30_ratio").status == "approved"


def test_an_amended_approval_is_refused(world: World) -> None:
    approve_new(world)
    sup.git(world.repo, "commit", "--amend", "-q", "-m", "amended", "--allow-empty")
    before = sup.state_dump(world.engine)
    with pytest.raises(ServiceError) as raised:
        world.life.sync()
    assert "rewritten" in raised.value.message
    assert sup.state_dump(world.engine) == before


def test_a_history_that_only_grew_is_fine(world: World) -> None:
    approve_new(world)
    raw_commit(world, {"README.txt": "hello"})
    assert world.life.sync() is not None


# --- I-2 and I-3 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attributes",
    [
        "*.yaml ident",
        "*.yaml working-tree-encoding=ISO-8859-1",
        "*.yaml working-tree-encoding=UTF-16",
        "*.yaml text eol=crlf",
        "*.yaml filter=evil",
    ],
)
def test_attributes_cannot_change_what_an_action_commits(world: World, attributes: str) -> None:
    raw_commit(world, {".gitattributes": attributes + "\n"})
    world.life.sync()
    marker = world.repo.path.parent / "filter-ran"
    sup.git(world.repo, "config", "filter.evil.clean", f"touch {marker}; cat")
    description = "A description with $Id: keep this text $ and café in it, long enough."
    result = world.life.create_proposal(
        world.caller("curator"), NEW, sup.metric_text(description=description), "attributes"
    )
    committed = world.repo.show(result.commit, NEW_DRAFT) or b""
    assert b"$Id: keep this text $" in committed and "café".encode() in committed
    assert b"\r\n" not in committed and not marker.exists()
    rows = sup.audit_rows(world.engine)
    mine = [r for r in rows if r["action"] == "create_proposal"]
    assert [r["commit_sha"] for r in mine] == [result.commit]
    assert mine[0]["files"] == {NEW_DRAFT: sha256(committed)}  # the intent hashes the blob
    assert not [
        r for r in rows if r["action"] == "external_change" and r["commit_sha"] == result.commit
    ]
    assert world.life.submit(world.caller("curator"), NEW, "and again").status == "in_review"


def test_a_linked_folder_gets_no_file_written_or_deleted_through_it(
    world: World, tmp_path: Path
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "npf_ratio.draft.yaml").write_text("keep me")
    (victim / "disbursement_amount.draft.yaml").write_text("keep me too")
    folder = world.repo.path / "lending" / "metrics"
    shutil.rmtree(folder)
    folder.symlink_to(victim)
    world.life.create_proposal(
        world.caller("curator"),
        CHANGED,
        sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="Changed")),
        "write",
    )
    world.life.withdraw(world.caller("curator"), CHANGED, "and remove it again")
    assert sorted(p.name for p in victim.iterdir()) == [
        "disbursement_amount.draft.yaml",
        "npf_ratio.draft.yaml",
    ]
    assert (victim / "npf_ratio.draft.yaml").read_text() == "keep me"
    assert (victim / "disbursement_amount.draft.yaml").read_text() == "keep me too"


def test_the_working_tree_is_brought_in_line_after_a_commit(world: World) -> None:
    approve_new(world)
    assert (world.repo.path / NEW_PATH).read_text() == (
        (world.repo.show(sup.head(world), NEW_PATH) or b"").decode()
    )
    assert not (world.repo.path / NEW_DRAFT).exists()
    assert sup.git(world.repo, "status", "--porcelain") == ""


# --- I-4 -------------------------------------------------------------------------------------


def customer_change(world: World, edit: Any, who: str = "curator") -> None:
    world.life.create_proposal(
        world.caller(who), CUSTOMER, sup.edited(sup.official_text(world, CUSTOMER), edit), "change"
    )
    world.life.submit(world.caller(who), CUSTOMER, "ready")


def _find(data: dict[str, Any], name: str) -> dict[str, Any]:
    found = next(d for d in data["dimensions"] if d["name"] == name)
    assert isinstance(found, dict)
    return found


def _rename_birth_year(data: dict[str, Any]) -> None:
    dimension = _find(data, "birth_year")
    dimension["name"] = "birth_yr"
    dimension.pop("sensitivity")
    dimension.pop("masking")


def _rename_national_id(data: dict[str, Any]) -> None:
    dimension = _find(data, "national_id")
    dimension["name"] = "nik_number"
    dimension.pop("sensitivity", None)


def _remove_full_name(data: dict[str, Any]) -> None:
    data["dimensions"] = [d for d in data["dimensions"] if d["name"] != "full_name"]


def _remove_birth_year(data: dict[str, Any]) -> None:
    data["dimensions"] = [d for d in data["dimensions"] if d["name"] != "birth_year"]


@pytest.mark.parametrize(
    ("edit", "catalog_blocks"),
    [
        (_rename_birth_year, True),
        (_rename_national_id, True),
        (_remove_full_name, False),
        (_remove_birth_year, False),
    ],
    ids=[
        "birth_year renamed without mask",
        "national_id renamed",
        "full_name removed",
        "birth_year removed",
    ],
)
def test_a_rename_or_a_removal_of_a_personal_dimension_needs_the_governance_team(
    world: World, edit: Any, catalog_blocks: bool
) -> None:
    customer_change(world, edit)
    for name in ("approver", "approver2", "admin"):
        with pytest.raises(ServiceError) as raised:
            world.life.approve(world.caller(name), CUSTOMER, "approve")
        assert raised.value.details["reason"] == "governance_team_needed"
        assert raised.value.details["needs"]
    governance = world.caller("governance")
    if catalog_blocks:
        # The right person may decide; and a column the catalog tags personal that the renamed
        # dimension declares lower is a reference issue (R03) of the proposal as well.
        with pytest.raises(ServiceError) as blocked:
            world.life.approve(governance, CUSTOMER, "governance agrees")
        assert blocked.value.code == LifecycleErrorCode.OPEN_REFERENCE_ISSUE
    else:
        assert world.life.approve(governance, CUSTOMER, "governance agrees").version == 2


# --- I-5 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["copy", "copy_draft", "move"])
def test_a_file_that_names_another_object_does_not_replace_it(world: World, how: str) -> None:
    token = world.token("admin")
    official = world.repo.show(sup.head(world), NPF_PATH) or b""
    if how == "copy":
        raw_commit(world, {"lending/metrics/zz_other.yaml": official.decode()})
        other = "metric:zz_other"
    elif how == "copy_draft":
        raw_commit(world, {"lending/metrics/zz_other.draft.yaml": official.decode()})
        other = "metric:zz_other"
    else:
        sup.git(world.repo, "mv", NPF_PATH, "lending/metrics/npf_ratio_moved.yaml")
        sup.git(world.repo, "commit", "-q", "-m", "move")
        other = "metric:npf_ratio_moved"
    synced = world.life.sync()
    assert synced is not None
    state, _ = synced
    assert state.objects[other].unreadable and state.objects[other].unreadable[0].code == "V02"
    row = sup.object_row(world, NPF)
    assert row["official"] is not None and row["status"] == "approved"
    assert not row["has_proposal"]
    assert not sup.has_object(world, other)
    assert world.services.get_definition(token, NPF).version == 1
    assert world.services.compile_query(token, {"metric": "npf_ratio"}).sql


def test_the_indexer_refuses_two_objects_with_one_id(world: World) -> None:
    from makna.definitions.loader import definition_objects, load_definitions
    from makna.search.embedding import HashProvider

    objects = definition_objects(load_definitions(DEFINITIONS))
    with pytest.raises(indexer.IndexerError):
        indexer.build(world.engine, HashProvider(), [*objects, objects[0]])


# --- task review: gaps -----------------------------------------------------------------------


def test_an_intent_is_not_completed_by_a_commit_with_other_content_even_with_the_actors_email(
    world: World,
) -> None:
    from sqlalchemy import insert

    from makna.index.tables import audit_event

    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "first")
    expected = (world.repo.show(sup.head(world), NEW_DRAFT) or b"").decode()
    with world.engine.begin() as connection:
        connection.execute(
            insert(audit_event).values(
                actor_user_id=world.ids["curator"],
                actor_label="curator@example.com",
                action="submit",
                object_id=NEW,
                files={
                    NEW_DRAFT: sha256(
                        expected.replace("status: draft", "status: in_review").encode()
                    )
                },
            )
        )
    # Same paths, the actor's own email, but other content.
    raw_commit(
        world,
        {NEW_DRAFT: expected.replace("status: draft", "status: in_review") + "# extra\n"},
        GIT_AUTHOR_EMAIL="curator@example.com",
    )
    world.life.sync()
    rows = sup.audit_rows(world.engine)
    assert not [r for r in rows if r["action"] == "submit" and r["commit_sha"]]
    assert [r["object_id"] for r in rows if r["action"] == "external_change"] == [NEW]
    assert [r for r in rows if r["action"] == "submit"][0]["failed_at"] is not None


def test_permission_is_decided_after_the_lock_on_the_state_the_lock_protects(world: World) -> None:
    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "first")
    world.life.submit(world.caller("curator"), NEW, "ready")
    outcome: list[Any] = []

    def waiting_approval() -> None:
        try:
            outcome.append(world.life.approve(world.caller("approver2"), NEW, "approve"))
        except ServiceError as error:
            outcome.append(error)

    with store.advisory_lock(world.engine):
        thread = threading.Thread(target=waiting_approval)
        thread.start()
        time.sleep(1.0)  # it has passed the early checks and is waiting for the lock
        # While it waits, approver2 becomes an author of the proposal (an outside commit).
        text_ = (world.repo.show(sup.head(world), NEW_DRAFT) or b"").decode()
        raw_commit(
            world, {NEW_DRAFT: text_ + "# touched\n"}, GIT_AUTHOR_EMAIL="approver2@example.com"
        )
    thread.join(60)
    (error,) = outcome
    assert isinstance(error, ServiceError)
    assert error.code == ServiceErrorCode.PERMISSION_DENIED
    assert error.details["reason"] == "author_cannot_approve"  # judged on the state after the lock


def test_the_role_and_teams_are_read_from_the_user_row_under_the_lock(world: World) -> None:
    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "first")
    world.life.submit(world.caller("curator"), NEW, "ready")
    stale = world.caller("approver")  # built when the user was an approver
    with world.engine.begin() as connection:
        connection.execute(
            update(app_user).where(app_user.c.id == world.ids["approver"]).values(role="viewer")
        )
    with pytest.raises(ServiceError) as raised:
        world.life.approve(stale, NEW, "still an approver?")
    assert raised.value.code == ServiceErrorCode.PERMISSION_DENIED
    assert sup.object_row(world, NEW)["status"] == "in_review"


def test_step_four_writes_everything_or_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "first")
    world.life.submit(world.caller("curator"), NEW, "ready")

    def index_tables() -> dict[str, list[Any]]:
        dump = sup.state_dump(world.engine)
        return {
            k: dump[k] for k in ("semantic_object", "object_edge", "search_document", "index_state")
        }

    before = index_tables()
    original = indexer._set_state

    def fail_late(connection: Any, key: str, value: str, result: Any) -> None:
        if key == "definitions_version":  # after the object and document rows are written
            raise RuntimeError("boom")
        original(connection, key, value, result)

    with monkeypatch.context() as patched:
        patched.setattr(indexer, "_set_state", fail_late)
        with pytest.raises(ServiceError) as raised:
            world.life.approve(world.caller("approver"), NEW, "approve")
    assert "makna index sync" in raised.value.message
    assert index_tables() == before  # no index row, no state, no last_indexed_commit changed
    open_rows = [r for r in sup.audit_rows(world.engine) if sup.is_open(r)]
    assert [(r["action"], r["commit_sha"]) for r in open_rows] == [("approve", None)]
    assert world.repo.show(sup.head(world), NEW_PATH) is not None  # git is ahead, and says so
    world.life.sync()
    assert sup.object_row(world, NEW)["status"] == "approved"
    assert not [r for r in sup.audit_rows(world.engine) if sup.is_open(r)]


# --- rulings: expected commit ----------------------------------------------------------------


def test_approve_and_reject_refuse_a_proposal_that_changed_after_it_was_reviewed(
    world: World,
) -> None:
    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "first")
    world.life.submit(world.caller("curator"), NEW, "ready")
    reviewed = str(sup.object_row(world, NEW)["proposal_commit"])
    assert reviewed == sup.head(world)
    text_ = (world.repo.show(sup.head(world), NEW_DRAFT) or b"").decode()
    raw_commit(world, {NEW_DRAFT: text_.replace("Total Contract Count", "Other Label")})
    world.life.sync()
    for call in (world.life.approve, world.life.reject):
        with pytest.raises(ServiceError) as raised:
            call(world.caller("approver"), NEW, "r", expected_proposal_commit=reviewed)
        assert raised.value.code == LifecycleErrorCode.CONFLICT
        assert "changed after you looked" in raised.value.message
    with pytest.raises(ServiceError) as raised:
        world.life.approve(world.caller("approver"), NEW, "r", expected_proposal_commit="not a sha")
    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    current = str(sup.object_row(world, NEW)["proposal_commit"])
    assert (
        world.life.approve(
            world.caller("approver"), NEW, "ok", expected_proposal_commit=current
        ).status
        == "approved"
    )


# --- minor findings --------------------------------------------------------------------------


def test_deeply_nested_yaml_is_a_definition_error_not_a_crash(world: World) -> None:
    with pytest.raises(ServiceError) as raised:
        world.life.create_proposal(world.caller("curator"), NEW, "[" * 20000, "deep")
    assert raised.value.code == LifecycleErrorCode.DEFINITION_INVALID
    raw_commit(world, {NEW_DRAFT: "[" * 20000})  # and as an outside file
    synced = world.life.sync()
    assert synced is not None and synced[0].objects[NEW].unreadable


def test_a_bad_gitmodules_file_and_a_huge_outside_file_do_not_stop_the_write_path(
    world: World,
) -> None:
    raw_commit(
        world, {".gitmodules": '[submodule "x"]\n\tpath = x\n\turl = ../x\n\tupdate = !echo hi\n'}
    )
    raw_commit(world, {NEW_DRAFT: "x: " + "a" * (300 * 1024)})
    started = time.monotonic()
    synced = world.life.sync()
    assert synced is not None
    assert "larger" in synced[0].objects[NEW].unreadable[0].message
    assert time.monotonic() - started < 30
    assert world.life.create_proposal(
        world.caller("curator"), CHANGED,
        sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="Fine")), "works",
    )  # fmt: skip


def test_the_owner_team_of_a_new_object_holds_on_every_edit(world: World) -> None:
    curator = world.caller("curator")
    world.life.create_proposal(curator, NEW, sup.metric_text(), "mine")
    with pytest.raises(ServiceError) as raised:
        world.life.update_proposal(
            curator, NEW, sup.metric_text(owner={"team": "marketing"}), "move"
        )
    assert raised.value.code == ServiceErrorCode.PERMISSION_DENIED
    assert raised.value.details["reason"] == "new_object_team_not_yours"


def test_a_lost_lock_stops_the_action_before_git_is_touched(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Lifecycle._write_intent

    def intent_then_kill_the_lock(self: Lifecycle, *args: Any, **kwargs: Any) -> int:
        intent = original(self, *args, **kwargs)
        killer = create_engine(world.url, isolation_level="AUTOCOMMIT")
        with killer.connect() as connection:  # only the lock of this database
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND database = "
                    "(SELECT oid FROM pg_database WHERE datname = current_database())"
                )
            )
        killer.dispose()
        time.sleep(0.3)
        return intent

    head, audit = sup.head(world), sup.audit_rows(world.engine)
    with monkeypatch.context() as patched:
        patched.setattr(Lifecycle, "_write_intent", intent_then_kill_the_lock)
        with pytest.raises(ServiceError) as raised:
            world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "r")
    assert raised.value.code == LifecycleErrorCode.CONFLICT and "lock" in raised.value.message
    assert sup.head(world) == head  # nothing was committed without the lock
    rows = sup.audit_rows(world.engine)
    assert len(rows) == len(audit) + 1 and rows[-1]["failed_at"] is not None
    assert world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "again")


def test_a_retry_after_a_crash_completes_its_own_intent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "first")
    world.life.submit(world.caller("curator"), NEW, "ready")
    original = Lifecycle._commit

    def crash(self: Lifecycle, *args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt  # the process dies after the intent row and before git

    with monkeypatch.context() as patched:
        patched.setattr(Lifecycle, "_commit", crash)
        with pytest.raises(KeyboardInterrupt):
            world.life.approve(world.caller("approver"), NEW, "first attempt")
    assert original is Lifecycle._commit
    result = world.life.approve(world.caller("approver"), NEW, "the retry")  # no error
    assert result.status == "approved"
    approvals = [r for r in sup.audit_rows(world.engine) if r["action"] == "approve"]
    assert [
        (r["reason"], r["commit_sha"] is not None, r["failed_at"] is not None) for r in approvals
    ] == [
        ("first attempt", False, True),
        ("the retry", True, False),
    ]
    assert approvals[1]["commit_sha"] == result.commit


def test_outside_author_emails_are_cleaned_before_the_audit_and_the_index(world: World) -> None:
    changed = sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="Outside"))
    raw_commit(
        world, {CHANGED_PATH: changed}, GIT_AUTHOR_EMAIL="evil\x1b[31mRED\x1b[0m‮@example.com"
    )
    world.life.sync()
    events = [r for r in sup.audit_rows(world.engine) if r["action"] == "external_change"]
    shown = " ".join(str(r["actor_label"]) for r in events)
    assert "\x1b" not in shown and "‮" not in shown
    row = sup.object_row(world, CHANGED)
    assert "\x1b" not in str(row["proposal_origin"]) and "‮" not in str(row["proposal_origin"])
    assert "\x1b" not in str(row["proposal"]) and "‮" not in str(row["proposal"])


def test_the_seed_does_not_write_into_a_planted_git_file(
    empty_database: URL, tmp_path: Path
) -> None:
    from makna.index import migrate

    migrate.upgrade(empty_database.render_as_string(hide_password=False))
    other = tmp_path / "other"
    other.mkdir()
    sup.git(GitRepo(other), "init", "-q")
    target = tmp_path / "target"
    target.mkdir()
    (target / ".git").write_text(f"gitdir: {other / '.git'}\n")
    engine = create_engine(empty_database)
    try:
        with pytest.raises(store.StoreError):
            seed_repository(engine, target, DEFINITIONS)
        assert sorted(p.name for p in target.iterdir()) == [".git"]
        assert not (other / "lending").exists()
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM audit_event")).scalar_one() == 0
    finally:
        engine.dispose()


def test_anything_unexpected_becomes_one_fixed_message_and_frees_the_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(self: Lifecycle, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("SECRET text from somewhere")

    with monkeypatch.context() as patched:
        patched.setattr(Lifecycle, "_plan", explode)
        with pytest.raises(ServiceError) as raised:
            world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "r")
    assert raised.value.code == ServiceErrorCode.INTERNAL
    assert "SECRET" not in str(raised.value)
    with store.advisory_lock(world.engine, wait_seconds=1):  # the lock was released
        pass


# --- C-2: git can show other content under a trusted sha ---------------------------------------


def forged_seed_replacement(world: World, scratch: Path) -> str:
    """`"<seed sha> <sha of a forged commit>"`: the forged commit exists as an object in the
    repository (made on a branch that is then dropped)."""
    seed = world.repo.log()[0].sha
    forged = sup.edited(sup.official_text(world, NPF), lambda d: d.update(label="FORGED label"))
    sup.git(world.repo, "checkout", "-q", "-b", "forge", seed)
    (world.repo.path / NPF_PATH).write_text(forged)
    (world.repo.path / "lending/metrics/zz_planted.yaml").write_text(
        sup.metric_text(name="zz_planted", status="approved", version=1)
    )
    sup.git(world.repo, "add", "-A")
    sup.git(world.repo, "commit", "-q", "-m", "forged seed")
    replacement = sup.git(world.repo, "rev-parse", "HEAD").strip()
    sup.git(world.repo, "checkout", "-q", "main")
    sup.git(world.repo, "branch", "-q", "-D", "forge")
    return f"{seed} {replacement}"


def assert_nothing_forged_is_served(world: World) -> None:
    token = world.token("admin")
    shown = world.services.get_definition(token, NPF)
    assert "FORGED" not in shown.label
    assert not sup.has_object(world, "metric:zz_planted")


def test_a_local_replace_ref_cannot_change_what_a_trusted_commit_contains(
    world: World, tmp_path: Path
) -> None:
    seed, replacement = forged_seed_replacement(world, tmp_path).split()
    sup.git(world.repo, "replace", seed, replacement)
    world.life.sync()
    assert_nothing_forged_is_served(world)
    assert sup.object_row(world, NPF)["official"]["label"] != "FORGED label"


def test_a_pushed_replace_ref_cannot_either(world: World, tmp_path: Path) -> None:
    seed, replacement = forged_seed_replacement(world, tmp_path).split()
    clone = tmp_path / "clone"
    sup.git(world.repo, "clone", "-q", str(world.repo.path), str(clone))
    clone_repo = GitRepo(clone)
    # The clone has the forged object only if it is fetched; push it from the original instead.
    sup.git(world.repo, "update-ref", f"refs/replace/{seed}", replacement)
    sup.git(clone_repo, "fetch", "-q", "origin", f"refs/replace/{seed}:refs/replace/{seed}")
    sup.git(world.repo, "update-ref", "-d", f"refs/replace/{seed}")
    sup.git(clone_repo, "push", "-q", "origin", f"refs/replace/{seed}:refs/replace/{seed}")
    assert f"refs/replace/{seed}" in sup.git(world.repo, "for-each-ref")
    world.life.sync()
    assert_nothing_forged_is_served(world)


@pytest.mark.parametrize("how", ["shallow", "grafts"])
def test_a_shallow_or_grafted_repository_is_refused(world: World, how: str) -> None:
    approve_new(world)
    head = sup.head(world)
    seed = world.repo.log()[0].sha
    before = sup.state_dump(world.engine)
    if how == "shallow":
        (world.repo.path / ".git" / "shallow").write_text(head + "\n")
    else:
        (world.repo.path / ".git" / "info").mkdir(exist_ok=True)
        (world.repo.path / ".git" / "info" / "grafts").write_text(f"{head} {seed}\n")
    with pytest.raises(ServiceError) as raised:
        world.life.sync()
    assert "shallow" in raised.value.message or "graft" in raised.value.message
    with pytest.raises(ServiceError):
        world.life.create_proposal(
            world.caller("curator"), "metric:x_one", sup.metric_text(name="x_one"), "r"
        )
    assert sup.state_dump(world.engine) == before
    assert world.services.get_definition(world.token("admin"), NPF).version == 1


def test_an_official_blob_rewritten_in_place_is_refused_by_its_audit_hash(world: World) -> None:
    import zlib

    approve_new(world)
    before = sup.state_dump(world.engine)
    oid = sup.git(world.repo, "rev-parse", f"HEAD:{NEW_PATH}").strip()
    loose = world.repo.path / ".git" / "objects" / oid[:2] / oid[2:]
    assert loose.exists()
    forged = b"label: forged\n"
    loose.chmod(0o644)
    loose.write_bytes(zlib.compress(b"blob %d\x00" % len(forged) + forged))
    with pytest.raises(ServiceError) as raised:
        world.life.sync()
    assert "audit" in raised.value.message
    assert sup.state_dump(world.engine) == before  # not served, not a fallback: the index stays
    assert sup.object_row(world, NEW)["official"]["label"] != "forged"
    with pytest.raises(ServiceError):
        world.life.deprecate(world.caller("approver"), NEW, "r")


def test_a_trusted_commit_whose_changes_differ_from_its_audit_record_is_refused(
    world: World,
) -> None:
    approve_new(world)
    head = sup.head(world)
    # A commit that has the same sha but a different parent (the graft effect), simulated by
    # asking the store to read with an audit record that names other files.
    from makna.index.tables import audit_event

    with world.engine.begin() as connection:
        connection.execute(
            update(audit_event)
            .where(audit_event.c.commit_sha == head)
            .values(files={"lending/metrics/other.yaml": "0" * 64})
        )
    with pytest.raises(ServiceError) as raised:
        world.life.sync()
    assert "audit" in raised.value.message


# --- I-1b: a merge commit does not get around the history check -------------------------------


def test_a_merge_with_the_indexed_head_as_second_parent_is_refused(world: World) -> None:
    approve_new(world)
    world.life.deprecate(world.caller("approver"), "metric:par30_ratio", "retired")
    seed = world.repo.log()[0].sha
    head = sup.head(world)
    tree = sup.git(world.repo, "rev-parse", f"{seed}^{{tree}}").strip()
    merge = sup.git(world.repo, "commit-tree", tree, "-p", seed, "-p", head, "-m", "merge").strip()
    sup.git(world.repo, "update-ref", "refs/heads/main", merge)
    before = sup.state_dump(world.engine)
    with pytest.raises(ServiceError) as raised:
        world.life.sync()
    assert "rewritten" in raised.value.message
    assert sup.state_dump(world.engine) == before
    assert world.services.get_definition(world.token("admin"), "metric:par30_ratio").status == (
        "deprecated"
    )


# --- minors ------------------------------------------------------------------------------------


def test_index_sync_checks_that_it_still_holds_the_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = store.HeldLock.check

    def counting(self: store.HeldLock) -> None:
        calls.append("check")
        original(self)

    monkeypatch.setattr(store.HeldLock, "check", counting)
    world.life.sync()
    assert calls


def test_the_working_tree_is_never_written_through_a_link_that_appears_between_steps(
    tmp_path: Path,
) -> None:
    from makna.services.lifecycle import _remove_file, _write_file

    root = tmp_path / "root"
    (root / "lending" / "metrics").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.yaml").write_text("keep")
    shutil.rmtree(root / "lending" / "metrics")
    (root / "lending" / "metrics").symlink_to(outside)
    with pytest.raises(OSError):
        _write_file(root, "lending/metrics/x.yaml", b"forged")
    with pytest.raises(OSError):
        _remove_file(root, "lending/metrics/x.yaml")
    (root / "lending" / "y.yaml").symlink_to(outside / "x.yaml")
    with pytest.raises(OSError):
        _write_file(root, "lending/y.yaml", b"forged")
    assert (outside / "x.yaml").read_text() == "keep"
    _write_file(root, "new/dir/z.yaml", b"fine")
    assert (root / "new/dir/z.yaml").read_bytes() == b"fine"
    _remove_file(root, "new/dir/z.yaml")
    assert not (root / "new/dir/z.yaml").exists()


def test_a_sibling_dimension_on_a_masked_personal_column_needs_the_governance_team(
    world: World,
) -> None:
    def add_month(data: dict[str, Any]) -> None:
        data["dimensions"].append(
            {
                "name": "birth_month",
                "type": "time",
                "expr": "birth_date",
                "description": "The month the customer was born.",
                "sensitivity": "personal",
                "masking": {"method": "truncate_date", "grain": "month"},
            }
        )

    customer_change(world, add_month)
    for name in ("approver", "approver2", "admin"):
        with pytest.raises(ServiceError) as raised:
            world.life.approve(world.caller(name), CUSTOMER, "approve")
        assert raised.value.details["reason"] == "governance_team_needed"
    assert (
        world.life.approve(world.caller("governance"), CUSTOMER, "governance agrees").version == 2
    )


# --- round 4 ---------------------------------------------------------------------------------


def test_a_fifo_at_the_target_path_cannot_hang_the_action_that_holds_the_lock(
    world: World,
) -> None:
    fifo = world.repo.path / NEW_DRAFT
    os.mkfifo(fifo)
    outcome: list[Any] = []

    def act() -> None:
        try:
            outcome.append(
                world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "fifo")
            )
        except ServiceError as error:
            outcome.append(error)

    thread = threading.Thread(target=act, daemon=True)
    thread.start()
    thread.join(20)
    if thread.is_alive():  # unblock the writer so the test does not leave a thread behind
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        os.close(fd)
        thread.join(5)
        pytest.fail("the action hung on a FIFO in the working tree")
    assert not isinstance(outcome[0], ServiceError)  # the commit is made from the checked bytes
    assert b"contract_count_total" in (world.repo.show(sup.head(world), NEW_DRAFT) or b"")
    with store.advisory_lock(world.engine, wait_seconds=2):  # the lock was released
        pass
    assert stat.S_ISFIFO(fifo.lstat().st_mode)  # the planted FIFO was left alone


def _break_seed_record(world: World, how: str) -> None:
    from makna.index.tables import audit_event

    seed = world.repo.log()[0].sha
    with world.engine.begin() as connection:
        files = connection.execute(
            text("SELECT files FROM audit_event WHERE commit_sha = :sha"), {"sha": seed}
        ).scalar_one()
        if how == "paths":
            files = {**{k: v for k, v in list(files.items())[1:]}}
        else:
            files = {**files, NPF_PATH: "0" * 64}
        connection.execute(
            update(audit_event).where(audit_event.c.commit_sha == seed).values(files=files)
        )


@pytest.mark.parametrize("how", ["paths", "hash"])
def test_accepting_a_rewritten_history_still_checks_the_audit_record(
    world: World, how: str
) -> None:
    approve_new(world)
    sup.git(world.repo, "reset", "-q", "--hard", world.repo.log()[0].sha)  # a rewrite
    _break_seed_record(world, how)
    before = sup.state_dump(world.engine)
    with pytest.raises(ServiceError) as raised:
        world.life.sync(accept_rewritten_history=True)
    assert "audit log recorded" in raised.value.message  # the record check, not the history one
    assert "history" not in raised.value.message
    assert sup.state_dump(world.engine) == before


def test_the_recorded_files_of_an_intent_claimed_by_its_own_action_are_checked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zlib

    from makna.search.embedding import HashProvider

    world.life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "first")
    world.life.submit(world.caller("curator"), NEW, "ready")
    with monkeypatch.context() as patched:
        patched.setattr(store, "reindex", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(ServiceError):
            world.life.approve(world.caller("approver"), NEW, "approve")
    intent = next(r for r in sup.audit_rows(world.engine) if sup.is_open(r))
    head = sup.head(world)
    oid = sup.git(world.repo, "rev-parse", f"HEAD:{NEW_PATH}").strip()
    loose = world.repo.path / ".git" / "objects" / oid[:2] / oid[2:]
    forged = b"label: forged\n"
    loose.chmod(0o644)
    loose.write_bytes(zlib.compress(b"blob %d\x00" % len(forged) + forged))
    with pytest.raises(store.StoreError) as raised:
        store.sync(
            world.engine, HashProvider(), world.repo, derive=None, claimed={intent["id"]: head}
        )
    assert "audit" in str(raised.value)
