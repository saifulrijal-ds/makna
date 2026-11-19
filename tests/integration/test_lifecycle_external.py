"""WP-18 criteria 2 and 9: no redeploy, and commits made outside the application (integration)."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from sqlalchemy import insert
from sqlalchemy.engine import URL

from makna.app import create_app
from makna.config import Settings
from makna.index.tables import app_user
from makna.services import ServiceError, ServiceErrorCode, accounts

from . import lifecycle_support as sup
from . import mcp_support as m
from .lifecycle_support import World

NEW = "metric:contract_count_total"
NEW_PATH = "lending/metrics/contract_count_total.yaml"
CHANGED = "metric:disbursement_amount"
CHANGED_PATH = "lending/metrics/disbursement_amount.yaml"
CHANGED_DRAFT = "lending/metrics/disbursement_amount.draft.yaml"
TRAILERS = (
    "\n\nMakna-Action: approve\nMakna-Object: metric:disbursement_amount\n"
    "Makna-Actor: 1\nMakna-Version: 9\n"
)


@pytest.fixture
def world(empty_database: URL, tmp_path: Path) -> Iterator[World]:
    built = sup.build_world(empty_database, tmp_path / "definitions")
    yield built
    built.services.dispose()


def raw_commit(
    world: World, path: str, text: str, message: str, email: str = "outside@example.com"
) -> str:
    (world.repo.path / path).parent.mkdir(parents=True, exist_ok=True)
    (world.repo.path / path).write_text(text)
    sup.git(world.repo, "add", path)
    sup.git(world.repo, "commit", "-q", "-m", message, env={"GIT_AUTHOR_EMAIL": email})
    return sup.head(world)


def test_a_new_metric_appears_for_an_agent_after_approval_in_the_same_running_application(
    world: World,
) -> None:
    settings = Settings.model_validate(
        {
            "database_url": world.url.render_as_string(hide_password=False),
            "secret_key": secrets.token_hex(32),
            "embedding_provider": "hash",
            "definitions_repo": str(world.repo.path),
            "mcp": {"allowed_hosts": [m.HOST]},
        }
    )
    token = accounts.create_token(world.engine, "viewer@example.com", "agent").token
    app = create_app(settings)

    async def call(client: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await client.call_tool(tool, arguments)
        parsed = json.loads(m.text_of(result))
        assert isinstance(parsed, dict)
        return parsed

    async def scenario() -> None:
        life = app.state.services.lifecycle
        async with m.serving(app):
            async with m.mcp_client(app, token) as client:
                query = {"query": "total contract count"}
                before = await call(client, "makna_search", query)
                assert NEW not in [h["id"] for h in before["hits"]]
                missing = await call(
                    client, "makna_compile_query", {"metric": "contract_count_total"}
                )
                assert "error" in missing
                life.create_proposal(world.caller("curator"), NEW, sup.metric_text(), "draft")
                life.submit(world.caller("curator"), NEW, "ready")
                drafts = await call(client, "makna_search", query)
                assert NEW not in [h["id"] for h in drafts["hits"]]  # not before approval
                life.approve(world.caller("approver"), NEW, "approved")
                after = await call(client, "makna_search", query)  # no restart in between
                assert NEW in [h["id"] for h in after["hits"]]
                compiled = await call(
                    client, "makna_compile_query", {"metric": "contract_count_total"}
                )
                assert "sql" in compiled and "error" not in compiled

    anyio.run(scenario)


def test_the_application_syncs_before_it_serves(world: World) -> None:
    raw_commit(
        world, CHANGED_PATH, sup.official_text(world, CHANGED) + "# touched\n", "a hand edit"
    )
    settings = Settings.model_validate(
        {
            "database_url": world.url.render_as_string(hide_password=False),
            "secret_key": secrets.token_hex(32),
            "embedding_provider": "hash",
            "definitions_repo": str(world.repo.path),
        }
    )
    app = create_app(settings)

    async def start() -> None:
        async with m.serving(app):
            pass

    anyio.run(start)
    assert [
        r["object_id"] for r in sup.audit_rows(world.engine) if r["action"] == "external_change"
    ]


@pytest.mark.parametrize("with_trailers", [False, True], ids=["plain message", "copied trailers"])
def test_a_raw_commit_on_an_approved_metric_is_a_proposal_in_review_never_served(
    world: World, with_trailers: bool
) -> None:
    svc, token = world.services, world.token("admin")
    viewer = world.caller("viewer", channel="mcp")
    old_label = svc.get_definition(viewer, CHANGED).label
    changed = sup.edited(
        sup.official_text(world, CHANGED),
        lambda d: d.update(label="Sneaky Label", status="approved", version=7),
    )
    message = "tweak the metric" + (TRAILERS if with_trailers else "")
    sha = raw_commit(world, CHANGED_PATH, changed, message)
    world.life.sync()

    events = [r for r in sup.audit_rows(world.engine) if r["action"] == "external_change"]
    assert [(r["object_id"], r["commit_sha"], r["actor_label"]) for r in events] == [
        (CHANGED, sha, "external:outside@example.com")
    ]
    for caller in (viewer, token):
        shown = svc.get_definition(caller, CHANGED)
        assert (shown.label, shown.version) == (old_label, 1)
    assert svc.compile_query(token, {"metric": "disbursement_amount"}).sql
    row = sup.object_row(world, CHANGED)
    assert row["has_proposal"] and row["proposal_status"] == "in_review"
    assert row["proposal_origin"] == "external:outside@example.com"
    assert row["proposal"]["drafted_by"] == "external:outside@example.com"
    assert row["proposal"]["status"] == "in_review" and row["official"]["label"] == old_label
    assert row["official_commit"] != sha  # the official content is still from an application commit
    curator_view = svc.get_definition(world.caller("curator", channel="mcp"), CHANGED)
    assert curator_view.proposal_status == "in_review"
    # A second sync logs nothing new.
    world.life.sync()
    assert len([r for r in sup.audit_rows(world.engine) if r["action"] == "external_change"]) == 1


def test_a_forged_trailer_does_not_make_a_commit_official(world: World) -> None:
    changed = sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="Forged"))
    raw_commit(world, CHANGED_PATH, changed, "approve(metric:disbursement_amount): ok" + TRAILERS)
    world.life.sync()
    assert sup.object_row(world, CHANGED)["official"]["label"] != "Forged"
    assert not [
        r
        for r in sup.audit_rows(world.engine)
        if r["action"] == "approve" and r["object_id"] == CHANGED
    ]


def test_an_uncommitted_edit_is_never_served(world: World) -> None:
    (world.repo.path / CHANGED_PATH).write_text("not even yaml: [\n")
    world.life.sync()
    assert world.services.get_definition(world.caller("viewer", "mcp"), CHANGED).version == 1
    assert not sup.object_row(world, CHANGED)["has_proposal"]


def test_approving_an_outside_change_serves_it_and_its_author_cannot(world: World) -> None:
    changed = sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="By Hand"))
    raw_commit(world, CHANGED_PATH, changed, "hand edit", email="approver2@example.com")
    world.life.sync()
    # The author of the outside commit (matched by email to a user) may not approve it.
    with pytest.raises(ServiceError) as raised:
        world.life.approve(world.caller("approver2"), CHANGED, "my own edit")
    assert raised.value.code == ServiceErrorCode.PERMISSION_DENIED
    assert raised.value.details["reason"] == "author_cannot_approve"
    result = world.life.approve(world.caller("approver"), CHANGED, "Reviewed the hand edit")
    assert result.version == 2
    shown = world.services.get_definition(world.token("admin"), CHANGED)
    assert (shown.label, shown.version) == ("By Hand", 2)
    assert not sup.object_row(world, CHANGED)["has_proposal"]
    last = [r for r in sup.audit_rows(world.engine) if r["action"] == "approve"][-1]
    assert last["commit_sha"] == sup.head(world)


def test_an_outside_author_without_an_account_still_blocks_a_user_with_that_email(
    world: World,
) -> None:
    changed = sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="By A Ghost"))
    raw_commit(world, CHANGED_PATH, changed, "edit", email="ghost@example.com")
    world.life.sync()
    with world.engine.begin() as connection:  # the person gets an account afterwards
        ghost_id = connection.execute(
            insert(app_user)
            .values(
                email="Ghost@Example.com".lower(),
                display_name="Ghost",
                password_hash="x",
                role="approver",
                teams=[],
            )
            .returning(app_user.c.id)
        ).scalar_one()
    from makna.services import Caller

    ghost = Caller(user_id=ghost_id, role="approver", channel="web")
    with pytest.raises(ServiceError) as raised:
        world.life.approve(ghost, CHANGED, "approving my own commit")
    assert raised.value.details["reason"] == "author_cannot_approve"
    assert world.life.approve(world.caller("approver"), CHANGED, "someone else").version == 2


def test_rejecting_an_outside_change_restores_the_official_file_and_keeps_a_draft(
    world: World,
) -> None:
    official_before = world.repo.show(sup.head(world), CHANGED_PATH)
    changed = sup.edited(
        sup.official_text(world, CHANGED), lambda d: d.update(label="Rejected Edit")
    )
    raw_commit(world, CHANGED_PATH, changed, "hand edit")
    world.life.sync()
    result = world.life.reject(world.caller("approver"), CHANGED, "Not what we agreed")
    assert result.status == "draft"
    head = sup.head(world)
    assert world.repo.show(head, CHANGED_PATH) == official_before  # as it was last approved
    draft = (world.repo.show(head, CHANGED_DRAFT) or b"").decode()
    assert "Rejected Edit" in draft and "status: draft" in draft
    row = sup.object_row(world, CHANGED)
    assert row["proposal_status"] == "draft"
    assert row["official"]["label"] != "Rejected Edit" and row["version"] == 1
    assert world.services.get_definition(world.token("admin"), CHANGED).version == 1


def test_withdrawing_an_outside_change_puts_the_official_file_back(world: World) -> None:
    official_before = world.repo.show(sup.head(world), CHANGED_PATH)
    changed = sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="Withdrawn"))
    raw_commit(world, CHANGED_PATH, changed, "hand edit")
    world.life.sync()
    world.life.withdraw(world.caller("approver"), CHANGED, "Not wanted")
    assert world.repo.show(sup.head(world), CHANGED_PATH) == official_before
    assert not sup.object_row(world, CHANGED)["has_proposal"]


def test_a_new_file_with_status_approved_added_by_a_raw_commit_is_not_visible_to_an_agent(
    world: World,
) -> None:
    text = sup.metric_text(status="approved", version=1)
    raw_commit(world, NEW_PATH, text, "add a metric")
    world.life.sync()
    row = sup.object_row(world, NEW)
    assert row["status"] == "in_review" and row["official"] is None
    token, viewer = world.token("admin"), world.caller("viewer", channel="mcp")
    for caller in (token, viewer):
        with pytest.raises(ServiceError) as raised:
            world.services.get_definition(caller, NEW)
        assert raised.value.code == ServiceErrorCode.NOT_FOUND
        assert NEW not in [h.id for h in world.services.search(caller, "total contract count").hits]
    from makna.model.request import QueryError

    with pytest.raises(QueryError):
        world.services.compile_query(token, {"metric": "contract_count_total"})
    # A curator sees it as a proposal in review; approving it in the application makes it official.
    seen = world.services.get_definition(world.caller("curator", channel="web"), NEW)
    assert seen.status == "in_review"
    approved = world.life.approve(world.caller("approver"), NEW, "Reviewed")
    assert (approved.status, approved.version) == ("approved", 1)
    assert world.services.get_definition(token, NEW).version == 1
    assert world.services.compile_query(token, {"metric": "contract_count_total"}).sql


def test_an_outside_edit_of_a_proposal_file_is_an_outside_proposal(world: World) -> None:
    text = sup.edited(sup.official_text(world, CHANGED), lambda d: d.update(label="Draft Label"))
    world.life.create_proposal(world.caller("curator"), CHANGED, text, "draft")
    draft = (world.repo.show(sup.head(world), CHANGED_DRAFT) or b"").decode()
    raw_commit(world, CHANGED_DRAFT, draft.replace("Draft Label", "Edited Outside"), "edit draft")
    world.life.sync()
    row = sup.object_row(world, CHANGED)
    assert row["proposal_origin"] == "external:outside@example.com"
    assert row["proposal_status"] == "in_review"
    # The author of the outside commit and the curator who started it are both authors.
    with pytest.raises(ServiceError):
        world.life.approve(world.caller("curator"), CHANGED, "r")
