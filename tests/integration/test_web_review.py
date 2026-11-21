"""WP-19B (WEB-3, GOV-1): the review queue `/review`, the review page with Approve and Reject, and
the audit page `/audit`, on the real application with a temporary git repository (integration
tier).

Acceptance 1 to 5 of the plan and the rulings: who may open what, who may decide (the buttons are
a hint, the lifecycle decides again), the commit the page showed, outside changes, rules and the
governance mark (D-17, D-20), format characters, escaping, bounds, and the audit page.
"""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import URL

from makna.definitions.render import MAX_YAML_BYTES
from makna.index.tables import audit_event
from makna.services import accounts, lifecycle, reviewing

from . import lifecycle_support as sup
from . import web_support as w
from .lifecycle_support import World
from .test_web_editor import Ctx, build, expected_of, form, post

CHANGED = "metric:disbursement_amount"  # owned by sales-performance, version 1
CHANGED_PATH = "lending/metrics/disbursement_amount.yaml"
CHANGED_DRAFT = "lending/metrics/disbursement_amount.draft.yaml"
CUSTOMER = "entity:customer"  # owned by data-governance; birth_year is personal and masked
SALES = ("sales-performance",)
OUTSIDE_EMAIL = "outside@example.com"
REVIEW_ROUTES = [
    ("GET", "/review"),
    ("GET", "/review/metric/disbursement_amount"),
    ("POST", "/review/metric/disbursement_amount/approve"),
    ("POST", "/review/metric/disbursement_amount/reject"),
    ("GET", "/audit"),
]


@pytest.fixture
def ctx(empty_database: URL, tmp_path: Path) -> Iterator[Ctx]:
    yield from build(empty_database, tmp_path / "definitions")


@pytest.fixture(scope="module")
def shared(test_database_url: URL, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Ctx]:
    """One world for the tests that only read. They must leave `HEAD` and the tables alone."""
    name = f"makna_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(test_database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield from build(test_database_url.set(database=name), tmp_path_factory.mktemp("repo"))
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# --- helpers ---------------------------------------------------------------------------------


def change_text(world: World, object_id: str = CHANGED, **changes: Any) -> str:
    """The official text of an object with its description lengthened (and `changes` applied)."""

    def edit(data: dict[str, Any]) -> None:
        data["description"] = data["description"] + " Reviewed."
        data.update(changes)

    return sup.edited(sup.official_text(world, object_id), edit)


def propose(
    ctx: Ctx,
    client: Any,
    object_id: str = CHANGED,
    text_: str | None = None,
    reason: str = "make it clearer",
) -> str:
    """Save and submit a proposal through the editor routes, as `client`'s user: the proposal's
    commit (what the review page sends back as `expected`)."""
    kind, _, name = object_id.partition(":")
    body = text_ if text_ is not None else change_text(ctx.world, object_id)
    done = form(client, f"/edit/{kind}/{name}/submit", body, reason=reason, expected="none")
    assert done.status_code == 303, done.text
    return sup.head(ctx.world)


def expected_commit(page: str) -> str:
    found = re.search(r'name="expected" value="([0-9a-f]{40})"', page)
    assert found, "no expected commit in the page"
    return found.group(1)


def decide(
    client: Any, kind: str, name: str, action: str, reason: str = "reviewed", **fields: str
) -> Any:
    """Approve or reject as the page does: the form of the page as it is now, so the commit it
    shows goes back; `fields` replace what the page carried."""
    sent = {"reason": reason, **fields}
    if "expected" not in sent:
        sent["expected"] = expected_commit(client.get(f"/review/{kind}/{name}").text)
    return post(client, f"/review/{kind}/{name}/{action}", **sent)


def outside_commit(
    world: World,
    path: str,
    new_text: str,
    *,
    message: str = "outside edit",
    email: str = OUTSIDE_EMAIL,
    name: str = "Outside Person",
) -> str:
    target = world.repo.path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text)
    sup.git(world.repo, "add", path)
    sup.git(
        world.repo,
        "commit",
        "-q",
        "-m",
        message,
        env={"GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_NAME": name},
    )
    return sup.head(world)


def outside_edit_of_official(world: World, object_path: str = CHANGED_PATH, **kw: Any) -> str:
    """A raw commit that changes the caveats of an official file, as someone outside Makna."""
    original = (world.repo.path / object_path).read_text()
    changed = sup.edited(original, lambda d: d.update(caveats="Changed from outside."))
    return outside_commit(world, object_path, changed, **kw)


def rows_of(page: str) -> list[str]:
    return re.findall(r'<tr data-object="([^"]+)"', page)


def audit_ids(page: str) -> list[int]:
    return [int(i) for i in re.findall(r'<tr data-id="(\d+)"', page)]


def plain(page: str) -> str:
    return html.unescape(page)


# --- who may open what -----------------------------------------------------------------------


def test_every_review_and_audit_route_needs_a_sign_in(shared: Ctx) -> None:
    client = w.make_client(shared.app)
    for method, path in REVIEW_ROUTES:
        response = client.request(method, path)
        assert response.status_code == 303, (method, path)
        assert response.headers["location"].startswith("/login")


def test_a_viewer_gets_403_on_every_review_and_audit_route(shared: Ctx) -> None:
    viewer = shared.client("viewer", teams=())
    before = sup.snapshot(shared.world)
    token = w.page_csrf(viewer)
    for method, path in REVIEW_ROUTES:
        response = (
            viewer.get(path)
            if method == "GET"
            else viewer.post(path, data={"csrf_token": token, "reason": "r", "expected": "0" * 40})
        )
        assert response.status_code == 403, (method, path)
    # Unknown kinds and names are a 403 for a viewer too: nothing tells a viewer what exists.
    for path in ("/review/nothing/x", "/review/metric/NOT_A_NAME", "/review/metric/no_such"):
        assert viewer.get(path).status_code == 403, path
        assert viewer.post(path + "/approve", data={"csrf_token": token}).status_code == 403, path
    for path in ("/audit?object=metric:npf_ratio", "/audit?before=3", "/audit?object=%3Cx%3E"):
        assert viewer.get(path).status_code == 403, path
    assert sup.snapshot(shared.world) == before


@pytest.mark.parametrize("role", ["curator", "approver", "admin"])
def test_curators_approvers_and_admins_open_review_and_audit(shared: Ctx, role: str) -> None:
    client = shared.client(role, teams=())
    assert client.get("/review").status_code == 200
    assert client.get("/audit").status_code == 200


def test_the_navigation_shows_review_and_audit_only_to_those_who_may_open_them(
    shared: Ctx,
) -> None:
    viewer, curator = shared.client("viewer", teams=()), shared.client()
    for page in (viewer.get("/account").text,):
        assert 'href="/review"' not in page and 'href="/audit"' not in page
    nav = curator.get("/account").text
    assert '<a href="/review">Review</a>' in nav and '<a href="/audit">Audit</a>' in nav


def test_every_post_needs_the_csrf_token(shared: Ctx) -> None:
    client = shared.client()
    before = sup.snapshot(shared.world)
    token = w.page_csrf(client)
    fields = {"reason": "r", "expected": "0" * 40}
    for method, path in REVIEW_ROUTES:
        if method != "POST":
            continue
        for label, extra in (
            ("none", {}),
            ("empty", {"csrf_token": ""}),
            ("wrong", {"csrf_token": "x" * 43}),
        ):
            response = client.post(path, data={**fields, **extra})
            assert response.status_code == 403, (path, label)
            assert "could not be verified" in response.text
        assert client.post(path, data=fields, params={"csrf_token": token}).status_code == 403
    assert sup.snapshot(shared.world) == before


def test_a_get_changes_nothing_and_a_bad_name_is_a_404_that_echoes_nothing(shared: Ctx) -> None:
    client = shared.client("approver", teams=())
    before = sup.snapshot(shared.world)
    marker = "marker_" + uuid.uuid4().hex[:8]
    for path in ("/review", "/audit", "/review?notice=approved", "/audit?object=metric:npf_ratio"):
        assert client.get(path).status_code == 200, path
    for path in (
        f"/review/metric/{marker}",
        "/review/nothing/x",
        "/review/metric/NOT_A_NAME",
        "/review/metric/%2e%2e",
        "/review/metric/a%2Fb",
        f"/review/{marker}/x",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert marker not in response.text
    token = w.page_csrf(client)
    for path in (f"/review/metric/{marker}/approve", "/review/nothing/x/reject"):
        response = client.post(
            path, data={"csrf_token": token, "reason": "r", "expected": "0" * 40}
        )
        assert response.status_code == 404, path
        assert marker not in response.text
    assert sup.snapshot(shared.world) == before


# --- criterion 1: the queue, the buttons, the forged POST ------------------------------------


def test_the_queue_lists_the_proposal_with_author_reason_difference_and_validation(
    ctx: Ctx,
) -> None:
    author = ctx.client("curator", SALES)
    commit = propose(ctx, author, reason="Say more about the amount")
    approver = ctx.client("approver", ())
    page = approver.get("/review").text
    assert rows_of(page) == [CHANGED]
    assert "1 proposal in review" in page
    # Author, reason, the summary of the difference with a link to all of it, the validation.
    assert f"<code>{author.user.email}</code>" in page
    assert 'data-field="reason">Say more about the amount<' in page
    assert re.search(r"\+1 .*?1 lines", page, re.S) or "+1 &minus;" in page
    assert 'href="/review/metric/disbursement_amount"' in page
    assert 'data-field="errors-none"' in page
    assert "Review and decide" in page and "You cannot decide" not in page
    # The item page: the full difference and the form, with the commit that is shown.
    item = approver.get("/review/metric/disbursement_amount").text
    assert 'data-field="diff"' in item and 'data-kind="changed"' in item
    assert "Reviewed." in item
    assert expected_commit(item) == commit
    assert 'data-action="approve"' in item and 'data-action="reject"' in item
    assert 'data-field="approve-ok"' in item
    assert 'data-field="commit">' + commit[:12] in item


def test_the_approve_button_is_for_those_who_may_decide_and_a_forged_post_is_a_403(
    ctx: Ctx,
) -> None:
    author = ctx.client("curator", SALES)
    propose(ctx, author)
    before = (sup.head(ctx.world), sup.audit_rows(ctx.engine))
    cannot = {
        "a curator outside the owner team": ctx.client("curator", ("marketing",)),
        "a curator of the owner team who wrote it": author,
    }
    for label, client in cannot.items():
        page = client.get("/review/metric/disbursement_amount").text
        assert 'data-action="approve"' not in page, label
        assert 'data-field="cannot-approve"' in page, label
        head = sup.head(ctx.world)  # a forged form has no page to read the commit from
        response = decide(client, "metric", "disbursement_amount", "approve", expected=head)
        assert response.status_code == 403, label
        assert 'data-code="PERMISSION_DENIED"' in response.text, label
        # A reject by the author is allowed (no two-person rule for reject), so only the team
        # outsider is refused for it.
    outsider = cannot["a curator outside the owner team"]
    assert 'data-action="reject"' not in outsider.get("/review/metric/disbursement_amount").text
    assert (
        decide(outsider, "metric", "disbursement_amount", "reject", expected=before[0])
    ).status_code == 403
    assert (sup.head(ctx.world), sup.audit_rows(ctx.engine)) == before
    # A colleague of the owner team may decide it.
    colleague = ctx.client("curator", SALES)
    assert 'data-action="approve"' in colleague.get("/review/metric/disbursement_amount").text
    approver = ctx.client("approver", ())
    assert 'data-action="approve"' in approver.get("/review/metric/disbursement_amount").text
    # The list says so too.
    assert "You cannot decide this" in outsider.get("/review").text


def test_the_author_cannot_approve_but_may_reject_their_own_proposal(ctx: Ctx) -> None:
    author = ctx.client("approver", ())
    propose(ctx, author, text_=change_text(ctx.world))
    page = author.get("/review/metric/disbursement_amount").text
    assert 'data-action="approve"' not in page and 'data-action="reject"' in page
    assert "You wrote part of this change" in page
    head = sup.head(ctx.world)
    forged = decide(author, "metric", "disbursement_amount", "approve", expected=head)
    assert forged.status_code == 403
    assert decide(author, "metric", "disbursement_amount", "reject").status_code == 303


# --- criterion 2: approve through the web route ----------------------------------------------


def test_after_approval_the_metric_page_shows_the_new_version(ctx: Ctx) -> None:
    author = ctx.client("curator", SALES)
    propose(ctx, author)
    approver = ctx.client("approver", ())
    assert "version 1" in approver.get("/metrics/disbursement_amount").text
    audits = len(sup.audit_rows(ctx.engine))
    response = decide(approver, "metric", "disbursement_amount", "approve", reason="Looks right")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/review?notice=approved")
    assert "version 2" in approver.get("/metrics/disbursement_amount").text
    rows = sup.audit_rows(ctx.engine)[audits:]
    assert [(r["action"], r["object_id"], r["reason"]) for r in rows] == [
        ("approve", CHANGED, "Looks right")
    ]
    assert rows[0]["actor_user_id"] == approver.user.id and rows[0]["commit_sha"] == sup.head(
        ctx.world
    )
    # The queue is empty, and its notice links to the page of the object.
    page = approver.get(response.headers["location"]).text
    assert "was approved" in page and 'data-field="empty"' in page
    assert 'href="/metrics/disbursement_amount"' in page
    # The proposal is gone: its review page is a 404 and a second decision is refused.
    assert approver.get("/review/metric/disbursement_amount").status_code == 404
    again = post(
        approver,
        "/review/metric/disbursement_amount/approve",
        reason="again",
        expected="0" * 40,
    )
    assert again.status_code in (404, 409)


def test_reject_makes_a_draft_again_and_is_audited(ctx: Ctx) -> None:
    propose(ctx, ctx.client("curator", SALES))
    approver = ctx.client("approver", ())
    response = decide(approver, "metric", "disbursement_amount", "reject", reason="Not yet")
    assert response.status_code == 303 and "notice=rejected" in response.headers["location"]
    assert "version 1" in approver.get("/metrics/disbursement_amount").text
    rows = sup.audit_rows(ctx.engine)
    assert (rows[-1]["action"], rows[-1]["to_status"], rows[-1]["reason"]) == (
        "reject",
        "draft",
        "Not yet",
    )
    assert rows_of(approver.get("/review").text) == []  # a draft is not in review any more
    assert approver.get("/review/metric/disbursement_amount").status_code == 404


# --- the commit the page showed --------------------------------------------------------------


def test_a_proposal_that_changed_after_the_page_was_opened_is_a_conflict(ctx: Ctx) -> None:
    author = ctx.client("curator", SALES)
    propose(ctx, author)
    approver = ctx.client("approver", ())
    stale = approver.get("/review/metric/disbursement_amount").text
    old = expected_commit(stale)
    # The author edits the proposal again and submits it again.
    newer = change_text(ctx.world, description="A completely different description for this.")
    page = author.get("/edit/metric/disbursement_amount").text
    form(author, "/edit/metric/disbursement_amount/submit", newer, reason="again",
         expected=expected_of(page))  # fmt: skip
    head, audits = sup.head(ctx.world), len(sup.audit_rows(ctx.engine))
    refused = post(
        approver, "/review/metric/disbursement_amount/approve", reason="ok", expected=old
    )
    assert refused.status_code == 409 and 'data-code="CONFLICT"' in refused.text
    assert "changed after you looked at it" in refused.text
    assert (sup.head(ctx.world), len(sup.audit_rows(ctx.engine))) == (head, audits)
    # The page now shows the new proposal and its commit, and the typed reason stays.
    assert "A completely different description" in refused.text
    assert expected_commit(refused.text) == head != old
    assert 'value="ok"' in refused.text
    # Reject is held to the same commit.
    assert (
        post(approver, "/review/metric/disbursement_amount/reject", reason="no", expected=old)
    ).status_code == 409
    assert sup.head(ctx.world) == head
    # With the commit of the new page it goes through.
    assert (
        post(approver, "/review/metric/disbursement_amount/approve", reason="ok",
             expected=head)
    ).status_code == 303  # fmt: skip


def test_a_form_without_a_commit_or_with_a_wrong_one_is_refused_and_nothing_changes(
    ctx: Ctx,
) -> None:
    propose(ctx, ctx.client("curator", SALES))
    approver = ctx.client("approver", ())
    head, audits = sup.head(ctx.world), len(sup.audit_rows(ctx.engine))
    for action in ("approve", "reject"):
        for expected in ("", "abc", "0" * 40, "G" * 40, "0" * 64, "x" * 5000):
            response = post(
                approver,
                f"/review/metric/disbursement_amount/{action}",
                reason="r",
                expected=expected,
            )
            assert response.status_code in (400, 409), (action, expected[:5])
        response = approver.post(
            f"/review/metric/disbursement_amount/{action}",
            data={"csrf_token": w.page_csrf(approver), "reason": "r"},
        )
        assert response.status_code == 400
    assert (sup.head(ctx.world), len(sup.audit_rows(ctx.engine))) == (head, audits)


def test_a_missing_or_too_long_reason_is_refused_with_the_form_kept(ctx: Ctx) -> None:
    propose(ctx, ctx.client("curator", SALES))
    approver = ctx.client("approver", ())
    head = sup.head(ctx.world)
    for reason in ("", "   "):
        response = decide(approver, "metric", "disbursement_amount", "approve", reason=reason)
        assert response.status_code == 400 and 'data-code="INVALID_INPUT"' in response.text
        assert 'data-action="approve"' in response.text  # the form is still there
    long = "r" * (lifecycle.MAX_REASON_LENGTH + 1)
    response = decide(approver, "metric", "disbursement_amount", "reject", reason=long)
    assert response.status_code == 400 and "at most 500 characters" in response.text
    assert f'value="{long[: lifecycle.MAX_REASON_LENGTH]}"' in response.text  # kept, cut to fit
    response = decide(approver, "metric", "disbursement_amount", "approve", reason="a\nb")
    assert response.status_code == 400
    assert sup.head(ctx.world) == head


# --- criterion 3: changes made outside Makna -------------------------------------------------


def test_an_outside_change_is_listed_with_its_commit_author_and_approved_by_another(
    ctx: Ctx,
) -> None:
    commit = outside_edit_of_official(ctx.world, message="Tidy the caveats\n\nlong body")
    approver = ctx.client("approver", ())
    # Listed straight from the repository, before `index sync` has even noticed the commit.
    page = approver.get("/review").text
    assert rows_of(page) == [CHANGED]
    assert 'data-field="outside-mark"' in page and "Changed outside Makna" in page
    assert 'data-field="outside-author">Outside Person<' in page
    assert f'data-field="outside-email">{OUTSIDE_EMAIL}<' in page
    assert 'data-field="outside-subject">Tidy the caveats<' in page
    assert "Git authorship is not verified" in page and commit[:12] in page
    assert "long body" not in page  # only the first line of the message
    item = approver.get("/review/metric/disbursement_amount").text
    assert "Changed outside Makna" in item and "Changed from outside." in item
    assert expected_commit(item) == commit
    # After a sync the recorded reason is there too, and the page labels it.
    ctx.world.life.sync()
    item = approver.get("/review/metric/disbursement_amount").text
    assert 'data-field="outside-recorded">changed outside Makna by Outside Person<' in item
    # Approve through the web route.
    response = decide(approver, "metric", "disbursement_amount", "approve", reason="Checked it")
    assert response.status_code == 303
    page = approver.get("/metrics/disbursement_amount").text
    assert "version 2" in page and "Changed from outside." in page
    rows = sup.audit_rows(ctx.engine)
    assert rows[-1]["action"] == "approve" and rows[-1]["actor_user_id"] == approver.user.id


def test_the_author_of_an_outside_change_cannot_approve_it_by_email(ctx: Ctx) -> None:
    outside_edit_of_official(ctx.world)
    # A user whose email is the commit author's, in the owner team and as an approver.
    mine = accounts.create_user(ctx.engine, OUTSIDE_EMAIL, "Same Email", "approver", (), w.PASSWORD)
    client = w.signed_in(ctx.app, mine)
    page = client.get("/review/metric/disbursement_amount").text
    assert 'data-action="approve"' not in page and "You wrote part of this change" in page
    head, audits = sup.head(ctx.world), len(sup.audit_rows(ctx.engine))
    response = decide(client, "metric", "disbursement_amount", "approve", expected=head)
    assert response.status_code == 403 and 'data-code="PERMISSION_DENIED"' in response.text
    assert (sup.head(ctx.world), len(sup.audit_rows(ctx.engine))) == (head, audits)
    # The same address in another case is the same author.
    upper = accounts.create_user(
        ctx.engine, "OUTSIDE@Example.COM".lower() + ".x", "Other", "approver", (), w.PASSWORD
    )
    assert upper.email != OUTSIDE_EMAIL  # a different address is not the author
    assert (
        'data-action="approve"'
        in w.signed_in(ctx.app, upper).get("/review/metric/disbursement_amount").text
    )


def test_rejecting_an_outside_change_makes_a_draft_and_restores_the_official_file(
    ctx: Ctx,
) -> None:
    original = (ctx.world.repo.path / CHANGED_PATH).read_text()
    outside_edit_of_official(ctx.world)
    approver = ctx.client("approver", ())
    assert (
        decide(approver, "metric", "disbursement_amount", "reject", reason="no").status_code == 303
    )
    head = sup.head(ctx.world)
    assert ctx.world.repo.show(head, CHANGED_PATH) == original.encode()
    assert b"Changed from outside." in (ctx.world.repo.show(head, CHANGED_DRAFT) or b"")
    assert "version 1" in approver.get("/metrics/disbursement_amount").text


def test_a_draft_made_by_rejecting_an_outside_change_counts_for_nobody(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap on open drafts (WP-19) charges a draft to the one who started it and may withdraw
    it. The draft a reject makes from an outside change is nobody's, whatever the audit says."""
    monkeypatch.setattr(lifecycle, "MAX_OPEN_DRAFTS_PER_USER", 1)
    first = ctx.client("curator", SALES)
    # `first` started a proposal for the object earlier, and it was approved.
    propose(ctx, first)
    approver = ctx.client("approver", ())
    assert decide(approver, "metric", "disbursement_amount", "approve").status_code == 303
    outside_edit_of_official(ctx.world)
    assert decide(approver, "metric", "disbursement_amount", "reject").status_code == 303
    # Neither the earlier author nor the one who rejected is at the cap because of that draft.
    for client in (first, approver):
        made = form(
            client,
            "/new/glossary_term/save",
            sup.edited(
                sup.official_text(ctx.world, "glossary_term:tenor"),
                lambda d, who=client.user.id: d.update(name=f"tenor_{who}", label="Tenor copy"),
            ),
            reason="starting another",
        )
        assert made.status_code == 303, made.text
    # The cap itself still works for what it counts: a second one for the first user is refused.
    again = form(
        first,
        "/new/glossary_term/save",
        sup.edited(
            sup.official_text(ctx.world, "glossary_term:tenor"),
            lambda d: d.update(name="tenor_more", label="Tenor more"),
        ),
        reason="one too many",
    )
    assert again.status_code == 409 and 'data-code="TOO_MANY_DRAFTS"' in again.text


# --- unreadable outside proposals ------------------------------------------------------------


def test_a_too_large_outside_proposal_is_unreadable_with_no_difference_and_no_dead_buttons(
    ctx: Ctx,
) -> None:
    huge = ctx.world.repo.path / "lending" / "metrics" / "huge_one.draft.yaml"
    huge.write_text("# " + "a" * (MAX_YAML_BYTES + 10) + "\n")
    sup.git(ctx.world.repo, "add", "-A")
    sup.git(ctx.world.repo, "commit", "-q", "-m", "outside huge file")
    admin = ctx.client("admin", ())
    page = admin.get("/review").text
    assert rows_of(page) == ["metric:huge_one"] and 'data-field="unreadable"' in page
    assert "No difference" in page and "Review and decide" not in page
    item = admin.get("/review/metric/huge_one").text
    assert 'data-field="unreadable"' in item and 'data-field="diff"' not in item
    assert 'data-action="approve"' not in item and 'data-action="reject"' not in item
    assert "shorten" not in item.lower()  # nothing tells a reviewer to shorten a file
    assert 'data-field="withdraw-link"' in item and 'href="/edit/metric/huge_one"' in item
    # A forged decision is the lifecycle's conflict, not a crash, and changes nothing.
    head = sup.head(ctx.world)
    for action in ("approve", "reject"):
        forged = post(admin, f"/review/metric/huge_one/{action}", reason="r", expected=head)
        assert forged.status_code == 409 and 'data-code="CONFLICT"' in forged.text
    assert sup.head(ctx.world) == head
    # A curator who did not write it sees it, and sees that they cannot withdraw it.
    curator = ctx.client("curator", ("marketing",))
    assert 'data-field="withdraw-link"' not in curator.get("/review/metric/huge_one").text


# --- criterion 4: the audit page -------------------------------------------------------------


def test_the_audit_page_shows_create_submit_and_approve_newest_first_and_filters_by_object(
    ctx: Ctx,
) -> None:
    author = ctx.client("curator", SALES)
    approver = ctx.client("approver", ())
    # A create and a submit through the editor, an approve through the review page.
    saved = form(author, "/edit/metric/disbursement_amount/save", change_text(ctx.world),
                 reason="first draft", expected="none")  # fmt: skip
    assert saved.status_code == 303
    page = author.get("/edit/metric/disbursement_amount").text
    form(author, "/edit/metric/disbursement_amount/submit", change_text(ctx.world),
         reason="ready", expected=expected_of(page))  # fmt: skip
    decide(approver, "metric", "disbursement_amount", "approve", reason="agreed")
    # Another object, to filter away.
    unrelated = sup.metric_text(owner={"team": "sales-performance"})
    form(author, "/new/metric/save", unrelated, reason="an unrelated draft")
    audit = approver.get("/audit").text
    ids = audit_ids(audit)
    assert ids == sorted(ids, reverse=True) and len(ids) == 5
    actions = re.findall(r'<tr data-id="\d+" data-action="([^"]+)"', audit)
    assert actions == ["create_proposal", "approve", "submit", "create_proposal", "seed"]
    only = approver.get("/audit?object=metric:disbursement_amount").text
    shown = re.findall(r'<tr data-id="\d+" data-action="([^"]+)"', only)
    assert shown == ["approve", "submit", "create_proposal"], shown
    assert "metric:contract_count_total" not in only
    assert "agreed" in only and "ready" in only and "first draft" in only
    assert f"{author.user.email}" in only and f"{approver.user.email}" in only
    assert 'href="/metrics/disbursement_amount"' in only
    # Time, statuses and the short commit are there.
    assert re.search(r"<code>[0-9a-f]{12}</code>", only) and "in_review &rarr; approved" in only
    # The action filter narrows too, and the two together.
    submits = approver.get("/audit?action=submit").text
    assert re.findall(r'<tr data-id="\d+" data-action="([^"]+)"', submits) == ["submit"]
    both = approver.get("/audit?object=metric:disbursement_amount&action=approve").text
    assert len(audit_ids(both)) == 1


def test_a_bad_audit_filter_is_a_400_that_echoes_nothing(shared: Ctx) -> None:
    client = shared.client("approver", ())
    marker = "marker_" + uuid.uuid4().hex[:8]
    for query in (
        f"object={marker}",
        f"object=metric:{marker.upper()}",
        "object=%3Cscript%3Ealert(1)%3C/script%3E",
        "object=" + "a" * 5000,
        f"action={marker}",
        "before=abc",
        "before=-5",
        "before=0",
        "before=" + "9" * 5000,
        "size=abc",
    ):
        response = client.get("/audit?" + query)
        assert response.status_code == 400, query
        assert marker not in response.text and marker.upper() not in response.text
        assert "<script>alert" not in response.text
        assert "metric:" not in response.text.split("<main>", 1)[1] or "metric:npf" in response.text
    # A name that is an id but no object is simply an empty result.
    empty = client.get(f"/audit?object=metric:{marker}")
    assert empty.status_code == 200 and 'data-field="empty"' in empty.text


def test_the_audit_page_is_bounded_and_older_rows_are_a_keyset_link(ctx: Ctx) -> None:
    author = ctx.client("curator", SALES)
    approver = ctx.client("approver", ())
    for index in range(4):  # four rows by the editor: create, then three updates
        page = author.get("/edit/metric/disbursement_amount").text
        form(author, "/edit/metric/disbursement_amount/save",
             change_text(ctx.world, description=f"A description number {index} for the page."),
             reason=f"step {index}", expected=expected_of(page))  # fmt: skip
    everything = audit_ids(approver.get("/audit").text)
    assert len(everything) >= 5
    first = approver.get("/audit?size=2").text
    assert audit_ids(first) == everything[:2]
    older = re.search(r'href="([^"]+)" data-field="older"', first)
    assert older, "no link to older rows"
    link = html.unescape(older.group(1))
    assert "before=" in link and "offset" not in link
    second = approver.get(link).text
    assert audit_ids(second) == everything[2:4]
    # Walking the links reaches every row once, newest first.
    seen: list[int] = []
    path = "/audit?size=2"
    for _ in range(50):
        page = approver.get(path).text
        seen += audit_ids(page)
        found = re.search(r'href="([^"]+)" data-field="older"', page)
        if not found:
            break
        path = html.unescape(found.group(1)) + "&size=2"
    assert seen == everything
    # The page size is bounded (100 at most, whatever is asked).
    assert len(audit_ids(approver.get("/audit?size=100000").text)) <= reviewing.MAX_AUDIT_PAGE


def test_a_failed_attempt_and_an_open_intent_are_marked_and_an_outside_actor_is_not_proof(
    ctx: Ctx,
) -> None:
    with ctx.engine.begin() as connection:
        connection.execute(
            insert(audit_event).values(
                actor_label="someone@example.com",
                action="approve",
                object_id=CHANGED,
                from_status="in_review",
                to_status="approved",
                reason="git failed at step 3",
                commit_sha=None,
                files={},
                failed_at=text("now()"),
            )
        )
        connection.execute(
            insert(audit_event).values(
                actor_label="someone@example.com",
                action="submit",
                object_id=CHANGED,
                reason="still open",
                commit_sha=None,
                files={},
            )
        )
    approver = ctx.client("approver", ())
    page = approver.get("/audit").text
    assert 'data-failed="yes"' in page and 'data-field="failed"' in page
    assert "it did not happen" in page and 'data-field="pending"' in page
    outside_edit_of_official(ctx.world)
    # `sync` logs the outside change as `external_change` and gives the open intent up.
    ctx.world.life.sync()
    page = approver.get("/audit").text
    assert (
        f"<mark>external:{OUTSIDE_EMAIL}</mark> <small>(named by a commit, not verified)"
        in plain(page)
    )
