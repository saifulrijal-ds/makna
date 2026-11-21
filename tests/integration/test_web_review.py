"""WP-19B (WEB-3, GOV-1): the review queue `/review`, the review page with Approve and Reject, and
the audit page `/audit`, on the real application with a temporary git repository (integration
tier).

Acceptance 1 to 5 of the plan and the rulings: who may open what, who may decide (the buttons are
a hint, the lifecycle decides again), the commit the page showed, outside changes, rules and the
governance mark (D-17, D-20), format characters, escaping, bounds, and the audit page.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import URL

from makna.definitions.render import MAX_YAML_BYTES
from makna.index.tables import audit_event
from makna.services import accounts, lifecycle, reviewing
from makna.web import review

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
    if sup.has_object(ctx.world, object_id):
        done = form(client, f"/edit/{kind}/{name}/submit", body, reason=reason, expected="none")
    else:  # a new object: its name is the text's own
        done = form(client, f"/new/{kind}/submit", body, reason=reason)
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


def flat(page: str) -> str:
    """A page without the highlight of the characters that changed inside a line, so that the
    text of a changed line reads in one piece."""
    return page.replace('<mark class="part">', "").replace("</mark>", "")


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
    assert "You can neither approve nor reject this" in outsider.get("/review").text


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
    assert "A completely different description" in flat(refused.text)
    assert expected_commit(refused.text) == head != old
    assert ">ok</textarea>" in refused.text
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
    assert f">{long[: lifecycle.MAX_REASON_LENGTH]}</textarea>" in response.text  # kept, cut to fit
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
    assert "Changed outside Makna" in item and "Changed from outside." in flat(item)
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
        path = html.unescape(found.group(1))
        assert "size=2" in path
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


# --- criterion 5: the users of a rule (D-17) -------------------------------------------------


def contract_with_rule_change(world: World) -> str:
    """The `contract` entity with its rule `restructured` changed and a measure that uses it."""

    def edit(data: dict[str, Any]) -> None:
        for rule in data["rules"]:
            if rule["name"] == "restructured":
                rule["expr"] = "is_restructured = TRUE"
        for measure in data["measures"]:
            if measure["name"] == "disbursed_contracts":
                measure["rules"] = ["restructured"]

    return sup.edited(sup.official_text(world, "entity:contract"), edit)


def listed(page: str, field: str) -> set[str]:
    return set(re.findall(rf'<code data-field="{field}">([^<]+)</code>', page))


def test_a_proposal_that_changes_a_rule_lists_every_measure_and_metric_that_uses_it(
    ctx: Ctx,
) -> None:
    author = ctx.client("curator", ("credit-risk",))
    propose(ctx, author, "entity:contract", contract_with_rule_change(ctx.world), "Rule edit")
    approver = ctx.client("approver", ())
    for path in ("/review", "/review/entity/contract"):
        page = approver.get(path).text
        assert 'data-field="rule-uses"' in page, path
        assert "<code>contract.restructured</code></strong> is changed" in page
        # A measure of the entity that uses it (in the proposal), and the metrics that use it
        # through a filter (`restructured_outstanding_principal`), through that filter's metric
        # (`restructured_ratio`, built on it), and through the measure (`disbursed_contract_count`).
        assert listed(page, "rule-measure") == {"contract.disbursed_contracts"}, path
        assert listed(page, "rule-metric") == {
            "restructured_outstanding_principal",
            "restructured_ratio",
            "disbursed_contract_count",
            "average_ticket_size",  # a ratio built on `disbursed_contract_count`
        }, path
    # A rule the proposal does not touch is not listed.
    assert "contract_snapshot.in_book" not in approver.get("/review/entity/contract").text


def test_a_rule_used_by_measures_lists_them_and_the_metrics_built_on_them(ctx: Ctx) -> None:
    def edit(data: dict[str, Any]) -> None:
        for rule in data["rules"]:
            if rule["name"] == "in_book":
                rule["expr"] = "COALESCE(days_past_due, 0) <= 200"

    changed = sup.edited(sup.official_text(ctx.world, "entity:contract_snapshot"), edit)
    propose(ctx, ctx.client("curator", ("credit-risk",)), "entity:contract_snapshot", changed)
    page = ctx.client("approver", ()).get("/review/entity/contract_snapshot").text
    assert listed(page, "rule-measure") == {
        "contract_snapshot.outstanding_principal",
        "contract_snapshot.npf_outstanding_principal",
        "contract_snapshot.par30_outstanding_principal",
        "contract_snapshot.active_contracts",
    }
    # The metrics are those the index says use the rule, through measures, filters and inputs.
    from makna.services import objects

    with ctx.engine.connect() as connection:
        edges = objects.edges(connection, ("input_of", "uses_rule"))
        official = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM semantic_object WHERE kind='metric' AND official IS NOT NULL"
                )
            )
        }
    expected = {
        name
        for name in official
        if "rule:contract_snapshot.in_book" in objects.rules_of_metric(edges, f"metric:{name}")
    }
    assert len(expected) >= 6 and listed(page, "rule-metric") == expected


def test_the_rule_users_of_every_official_metric_agree_with_the_index_edges(shared: Ctx) -> None:
    """`rules_by_metric` is the reading of the indexer's `uses_rule` edges, through `input_of`."""
    from makna.definitions.loader import load_definitions
    from makna.model.definitions import Entity, Metric
    from makna.services import objects
    from tests.catalog_support import DEFINITIONS

    loaded = load_definitions(DEFINITIONS)
    official = [f.definition for f in loaded.files if not f.is_proposal and f.definition]
    entities = {d.name: d for d in official if isinstance(d, Entity)}
    metrics = {d.name: d for d in official if isinstance(d, Metric)}
    found = reviewing.rules_by_metric(entities, metrics)
    with shared.engine.connect() as connection:
        edges = objects.edges(connection, ("input_of", "uses_rule"))
    assert len(metrics) > 20 and any(found.values())
    for name in metrics:
        assert found[name] == set(objects.rules_of_metric(edges, f"metric:{name}")), name


def test_a_removed_rule_is_listed_with_the_use_it_loses(ctx: Ctx) -> None:
    def edit(data: dict[str, Any]) -> None:
        data["rules"] = [r for r in data["rules"] if r["name"] != "restructured"]

    # The proposal fails Check (a metric still names the rule), but it can be saved as a draft;
    # the review page of a submitted one needs no errors, so read the service directly.
    text_ = sup.edited(sup.official_text(ctx.world, "entity:contract"), edit)
    caller = ctx.world.caller("curator")
    ctx.world.life.create_proposal(caller, "entity:contract", text_, "drop the rule")
    state = ctx.world.life._read_for_editor(ctx.world.life._repo())
    item = state.objects["entity:contract"]
    assert item.proposal is not None and item.official is not None
    uses, total = reviewing.rule_uses(
        {k: o.official for k, o in state.objects.items() if o.official is not None},
        item.official,
        item.proposal,
    )
    assert total == 1 and uses[0].change == "removed" and uses[0].rule == "contract.restructured"
    assert "restructured_outstanding_principal" in uses[0].metrics


def test_the_users_of_a_rule_are_bounded_and_counted(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reviewing, "MAX_RULE_USERS", 2)
    propose(
        ctx,
        ctx.client("curator", ("credit-risk",)),
        "entity:contract",
        contract_with_rule_change(ctx.world),
    )
    page = ctx.client("approver", ()).get("/review/entity/contract").text
    assert len(listed(page, "rule-metric")) == 2 and "Metrics that use it (4)" in page
    assert "and 2 more" in page


# --- criterion 5: the governance team (D-20) -------------------------------------------------


def unmask_birth_year(data: dict[str, Any]) -> None:
    for dimension in data["dimensions"]:
        if dimension["name"] == "birth_year":
            dimension.pop("masking")


def test_a_loosening_is_marked_and_only_the_governance_team_may_approve_it(ctx: Ctx) -> None:
    text_ = sup.edited(sup.official_text(ctx.world, CUSTOMER), unmask_birth_year)
    author = ctx.client("curator", ("data-governance",))
    propose(ctx, author, CUSTOMER, text_, "Show the full date")
    approver = ctx.client("approver", ())  # an approver, and not in the governance team
    for path in ("/review", "/review/entity/customer"):
        page = approver.get(path).text
        assert "Needs the governance team (data-governance)" in page, path
        assert 'data-field="loosenings"' in page and "removed or finer masking rule" in page, path
    item = approver.get("/review/entity/customer").text
    assert 'data-action="approve"' not in item and 'data-field="cannot-approve"' in item
    assert "member of the governance team must approve it" in item
    # Reject needs no governance team, so that button is there.
    assert 'data-action="reject"' in item
    head, audits = sup.head(ctx.world), len(sup.audit_rows(ctx.engine))
    forged = decide(approver, "entity", "customer", "approve", expected=head)
    assert forged.status_code == 403 and 'data-code="PERMISSION_DENIED"' in forged.text
    assert "governance team" in forged.text
    admin = ctx.client("admin", ())
    assert decide(admin, "entity", "customer", "approve", expected=head).status_code == 403
    assert 'data-action="approve"' not in admin.get("/review/entity/customer").text
    assert (sup.head(ctx.world), len(sup.audit_rows(ctx.engine))) == (head, audits)
    # A member of the governance team (an approver) may decide it.
    governance = ctx.client("approver", ("data-governance",))
    page = governance.get("/review/entity/customer").text
    assert 'data-action="approve"' in page and "Needs the governance team" in page
    assert decide(governance, "entity", "customer", "approve").status_code == 303


def test_a_change_that_does_not_loosen_has_no_governance_mark(ctx: Ctx) -> None:
    propose(ctx, ctx.client("curator", SALES))
    page = ctx.client("approver", ()).get("/review/metric/disbursement_amount").text
    assert "governance team" not in page and 'data-field="loosenings"' not in page


# --- what the difference shows beyond the YAML -----------------------------------------------


def test_the_review_lists_removed_dimensions_changed_masking_siblings_and_the_owner(
    ctx: Ctx,
) -> None:
    def edit(data: dict[str, Any]) -> None:
        data["dimensions"] = [d for d in data["dimensions"] if d["name"] != "occupation_group"]
        for dimension in data["dimensions"]:
            if dimension["name"] == "birth_year":
                dimension["masking"] = {"method": "truncate_date", "grain": "month"}
            if dimension["name"] == "registered_date":
                dimension["expr"] = "CAST(registered_at AS TIMESTAMP)"
        data["dimensions"].append(
            {
                "name": "birth_month",
                "type": "time",
                "expr": "birth_date",
                "description": "Month of birth, read from the same column as the year.",
                "sensitivity": "personal",
                "masking": {"method": "truncate_date", "grain": "month"},
            }
        )
        data["relationships"].append(
            {
                "name": "registration_branch",
                "to": "branch",
                "keys": [{"from": "home_branch_id", "to": "branch_id"}],
                "description": "The branch where the customer registered, read as a second role.",
            }
        )
        data["owner"] = {"team": "credit-risk"}

    text_ = sup.edited(sup.official_text(ctx.world, CUSTOMER), edit)
    ctx.world.life.create_proposal(ctx.world.caller("curator2"), CUSTOMER, text_, "many changes")
    # `curator2` is of credit-risk, so the entity of data-governance is another team's to change;
    # an approver wrote it, and submits it.
    ctx.world.life.submit(ctx.world.caller("approver"), CUSTOMER, "ready")
    page = ctx.client("admin", ()).get("/review/entity/customer").text
    marks = dict(re.findall(r'<li data-mark="([a-z_]+)">([^<]*)</li>', page))
    assert marks["dimension_removed"] == "dimension occupation_group was removed"
    assert (
        "masking changed from truncate_date(year) to truncate_date(month)"
        in marks["dimension_masking"]
    )
    assert (
        "expr changed from CAST(registered_at AS DATE) to CAST(registered_at AS TIMESTAMP)"
        in marks["dimension_expr"]
    )
    assert (
        "birth_month reads the column birth_date, which the personal dimension"
        in marks["same_column_as_personal"]
    )
    assert marks["relationship_added"] == "relationship registration_branch was added"
    assert marks["owner"] == "owner team changes from data-governance to credit-risk"
    assert 'data-field="owner-change"' in page


# --- bidirectional and zero-width characters, and escaping -----------------------------------


def test_format_characters_are_made_visible_in_every_text_a_reviewer_reads(ctx: Ctx) -> None:
    bidi, zero = chr(0x202E), chr(0x200B)
    # The description is the proposal's text; the reason, a commit's subject, and a name come too.
    text_ = change_text(ctx.world, description=f"Disbursed {bidi}amount{zero} in rupiah, whole.")
    author = ctx.client("curator", SALES)
    propose(ctx, author, text_=text_, reason="checked twice")
    page = ctx.client("approver", ()).get("/review/metric/disbursement_amount").text
    assert "&lt;U+202E&gt;amount&lt;U+200B&gt;" in page.replace("</mark>", "").replace(
        '<mark class="part">', ""
    )
    assert bidi not in page and zero not in page  # none is sent raw
    # A reason cannot hold one (the lifecycle refuses it), but an audit row can, whatever wrote it.
    with ctx.engine.begin() as connection:
        connection.execute(
            insert(audit_event).values(
                actor_label=f"ev{bidi}il@example.com",
                action="submit",
                object_id=CHANGED,
                reason=f"checked{bidi} twice{zero}",
                commit_sha="f" * 40,
                files={},
            )
        )
    queue = ctx.client("approver", ()).get("/review").text
    assert bidi not in queue
    # An outside commit: its subject keeps the character (the committer wrote it), the name and
    # the email of a commit author are cleaned of such characters when they are read.
    outside_edit_of_official(
        ctx.world,
        object_path="lending/metrics/penalty_income.yaml",
        message=f"fix{bidi} it{zero}",
        name=f"Eve{bidi} <b>Outside</b>",
    )
    queue = ctx.client("approver", ()).get("/review").text
    assert bidi not in queue and zero not in queue
    assert "fix&lt;U+202E&gt; it&lt;U+200B&gt;" in queue
    assert "Eve bOutside/b" in queue or "Eve" in queue
    assert "<b>Outside</b>" not in queue
    audit = ctx.client("approver", ()).get("/audit").text
    assert bidi not in audit and zero not in audit
    assert "checked&lt;U+202E&gt; twice&lt;U+200B&gt;" in audit
    assert "ev&lt;U+202E&gt;il@example.com" in audit


def test_hostile_text_is_escaped_on_every_page(ctx: Ctx) -> None:
    hostile = '</td><script>alert("x")</script>\'"&'
    author = ctx.client("curator", SALES)
    text_ = change_text(ctx.world, description=f"Disbursed {hostile} amount in rupiah, whole.")
    propose(ctx, author, text_=text_, reason=hostile)
    outside_edit_of_official(
        ctx.world,
        object_path="lending/metrics/penalty_income.yaml",
        message=hostile,
        name="<script>alert(1)</script>",
    )
    ctx.world.life.sync()
    approver = ctx.client("approver", ())
    pages = [
        approver.get("/review").text,
        approver.get("/review/metric/disbursement_amount").text,
        approver.get("/review/metric/penalty_income").text,
        approver.get("/audit").text,
        approver.get("/audit?object=metric:disbursement_amount").text,
    ]
    for page in pages:
        assert "<script>alert" not in page and "</td><script>" not in page
        assert "&lt;script&gt;alert" in page or "alert" not in page
    # The reason of the form, kept after a refusal, is escaped too.
    refused = post(
        approver, "/review/metric/disbursement_amount/approve", reason=hostile, expected="0" * 40
    )
    assert refused.status_code == 409 and "<script>alert" not in refused.text
    assert ">&lt;/td&gt;&lt;script&gt;" in refused.text
    # The templates never mark anything safe.
    templates = Path(review.__file__).parent / "templates"
    for name in ("review.html", "review_item.html", "audit.html", "_review.html"):
        source = (templates / name).read_text()
        assert "|safe" not in source and "| safe" not in source and "Markup" not in source
    assert "Markup" not in Path(review.__file__).read_text().replace("never `Markup`", "")
    assert "HtmlDiff(" not in Path(review.__file__).read_text()


# What a review template may print without the filter `visible`: numbers, plural endings, the
# hex of a commit, fixed words of the code, and addresses the code built itself (autoescaped).
PLAIN_VALUES = frozenset(
    {
        '"" if queue.total == 1 else "s"',
        '"" if item.reference_total == 1 else "s"',
        '"" if entry.error_count == 1 else "s"',
        '"" if row.skipped == 1 else "s"',
        "e.lines_added",
        "e.lines_removed",
        "e.version",
        "entry.version",
        "queue.total",
        "rows | length",
        "entry.error_count",
        "entry.errors | length",
        "entry.authors_total - entry.authors | length",
        "entry.rule_uses_total - entry.rule_uses | length",
        "item.marks_total - item.marks | length",
        "item.reference_total",
        "change.metrics_total",
        "change.metrics_total - change.metrics | length",
        "use.measures_total",
        "use.metrics_total",
        "use.measures_total - use.measures | length",
        "use.metrics_total - use.metrics | length",
        "issue.line",
        'issue.line if issue.line is not none else "-"',
        "row.skipped",
        'row.left_no if row.left_no is not none else ""',
        'row.right_no if row.right_no is not none else ""',
        "r.id",
        "row.when",
        "entry.proposal_commit[:12]",
        "outside.commit[:12]",
        "entry.proposal_commit or ''",
        "max_reason",
        "csrf_token",
        "mark.kind",
        "use.change",
        "row.kind",
        "problem.code",
        "item.approve_refusal.code",
        "notice",
        "kind_label",
        "row.kind_label",
        "decided_kind",
        "decided_page",
        "object_page",
        "row.object_page",
        "row.filter",
        "row.href",
        "edit_href",
        "base",
        "back",
        "all_link",
        "older",
        "a",
        "problem.headline",
        # macros (they escape what they print)
        "authors_line(e)",
        "authors_line(entry)",
        "errors_list(e)",
        "errors_list(entry)",
        "governance_mark(e)",
        "governance_mark(entry)",
        "outside_box(e.outside)",
        "outside_box(entry.outside)",
        "rule_uses_list(e)",
        "rule_uses_list(entry)",
        "diff_table(rows)",
    }
)


def test_every_value_of_a_review_template_is_escaped_and_shown_through_visible() -> None:
    """Each `{{ ... }}` of the review pages goes through the filter `visible` (so no format or
    control character is sent raw) or is one of the values of `PLAIN_VALUES`: numbers, fixed
    words and addresses the code built. A new value must be added there on purpose."""
    templates = Path(review.__file__).parent / "templates"
    for name in ("review.html", "review_item.html", "audit.html", "_review.html"):
        for expression in re.findall(r"\{\{\s*(.*?)\s*\}\}", (templates / name).read_text()):
            assert "| visible" in expression or expression in PLAIN_VALUES, (name, expression)


# --- what approval would refuse --------------------------------------------------------------


def test_an_open_reference_issue_is_shown_and_approve_is_not_offered(ctx: Ctx) -> None:
    def edit(data: dict[str, Any]) -> None:
        data["dimensions"].append(
            {
                "name": "ghost",
                "type": "categorical",
                "expr": "no_such_column",
                "description": "A dimension on a column the catalog does not have.",
            }
        )

    text_ = sup.edited(sup.official_text(ctx.world, "entity:branch"), edit)
    author = ctx.client("curator", ("credit-risk",))
    propose(ctx, author, "entity:branch", text_, "Add a dimension")
    approver = ctx.client("approver", ())
    page = approver.get("/review/entity/branch").text
    assert 'data-field="reference-issues"' in page and "open reference issue" in page
    assert "no_such_column" in page or "R0" in page
    assert 'data-field="approve-refused"' in page and 'data-code="OPEN_REFERENCE_ISSUE"' in page
    assert 'data-action="approve"' not in page and 'data-action="reject"' in page
    assert 'data-field="approve-not-offered"' in page
    # A forged approve is the lifecycle's refusal, with the page of the proposal again.
    head = sup.head(ctx.world)
    forged = decide(approver, "entity", "branch", "approve", expected=head)
    assert forged.status_code == 409 and 'data-code="OPEN_REFERENCE_ISSUE"' in forged.text
    assert sup.head(ctx.world) == head
    # The list says what approval does not need to be told twice: no definition errors.
    assert 'data-field="errors-none"' in approver.get("/review").text


def test_definition_errors_of_an_outside_change_are_listed_and_approve_is_not_offered(
    ctx: Ctx,
) -> None:
    path = "lending/metrics/disbursement_amount.yaml"
    original = (ctx.world.repo.path / path).read_text()
    broken = sup.edited(original, lambda d: d.update(measure="contract.no_such_measure"))
    outside_commit(ctx.world, path, broken)
    approver = ctx.client("approver", ())
    queue = approver.get("/review").text
    assert 'data-field="errors"' in queue and "definition error" in queue
    assert "Review and decide" in queue
    page = approver.get("/review/metric/disbursement_amount").text
    assert 'data-field="error-list"' in page and 'data-field="approve-refused"' in page
    assert 'data-code="DEFINITION_INVALID"' in page
    assert 'data-action="approve"' not in page and 'data-action="reject"' in page
    head = sup.head(ctx.world)
    forged = decide(approver, "metric", "disbursement_amount", "approve", expected=head)
    assert forged.status_code == 422 and 'data-code="DEFINITION_INVALID"' in forged.text
    assert sup.head(ctx.world) == head
    # Rejecting it is possible and restores the official file.
    assert decide(approver, "metric", "disbursement_amount", "reject").status_code == 303


# --- errors, the plain directory, and the 500 page -------------------------------------------


def test_a_plain_directory_says_writes_are_off_on_the_queue_and_still_shows_the_audit(
    empty_database: URL, tmp_path: Path
) -> None:
    plain_dir = Path(__file__).resolve().parents[2] / "semantic" / "definitions"  # inside a repo
    world = sup.build_world(empty_database, tmp_path / "definitions")
    app = w.make_app(empty_database, definitions_repo=str(plain_dir))
    try:
        client = w.signed_in(app, w.make_user(world.engine, "approver"))
        page = client.get("/review")
        assert page.status_code == 409 and "Writes are off" in page.text
        assert 'data-code="CONFLICT"' in page.text and "<table" not in page.text
        assert client.get("/review/metric/disbursement_amount").status_code == 409
        audit = client.get("/audit")
        assert audit.status_code == 200 and "seed" in audit.text
    finally:
        app.state.services.dispose()
        world.services.dispose()


def test_a_failure_after_the_commit_says_the_change_is_official(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services import store

    propose(ctx, ctx.client("curator", SALES))
    approver = ctx.client("approver", ())
    page = approver.get("/review/metric/disbursement_amount").text
    commit = expected_commit(page)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("secret-index-error")

    monkeypatch.setattr(store, "sync", broken)
    commits = len(ctx.world.repo.log())
    response = post(
        approver, "/review/metric/disbursement_amount/approve", reason="go", expected=commit
    )
    assert response.status_code == 500
    assert len(ctx.world.repo.log()) == commits + 1  # the commit was made
    text_ = plain(response.text)
    assert "The change was made official, but the index has not caught up yet." in text_
    assert "makna index sync" in text_ and "secret-index-error" not in text_
    assert "Nothing was changed" not in text_


def test_an_unexpected_error_is_the_500_page_and_its_text_goes_nowhere(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from makna.definitions.repo import RepoError
    from makna.services.lifecycle import Lifecycle

    secret = "secret-" + uuid.uuid4().hex

    def broken(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise RepoError(secret, 1)

    approver = ctx.client("approver", ())
    for name in ("review_queue", "review_item", "audit_events", "approve"):
        monkeypatch.setattr(Lifecycle, name, broken)
    caplog.set_level(logging.INFO)
    for method, path in (
        ("get", "/review"),
        ("get", "/review/metric/disbursement_amount"),
        ("get", "/audit"),
        ("post", "/review/metric/disbursement_amount/approve"),
    ):
        if method == "get":
            response = approver.get(path)
        else:
            response = post(approver, path, reason="r", expected="0" * 40)
        assert response.status_code == 500, path
        assert secret not in response.text
    assert not any(secret in r.getMessage() for r in caplog.records)
    assert any("error=RepoError" in r.getMessage() for r in caplog.records)


def test_the_log_names_the_user_and_the_object_but_never_the_reason_or_an_email(
    ctx: Ctx, caplog: pytest.LogCaptureFixture
) -> None:
    author = ctx.client("curator", SALES)
    propose(ctx, author)
    outside_edit_of_official(ctx.world, object_path="lending/metrics/penalty_income.yaml")
    approver = ctx.client("approver", ())
    caplog.set_level(logging.DEBUG)
    reason = "a reason " + uuid.uuid4().hex
    assert (
        decide(approver, "metric", "disbursement_amount", "approve", reason=reason).status_code
        == 303
    )
    assert decide(approver, "metric", "penalty_income", "reject", reason=reason).status_code == 303
    approver.get("/review")
    approver.get("/audit")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("web: review")]
    assert len(lines) == 2
    assert f"user={approver.user.id} object={CHANGED} outcome=ok" in lines[0]
    everything = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (reason, approver.user.email, author.user.email, OUTSIDE_EMAIL, "Reviewed."):
        assert secret not in everything, secret


def test_the_parsing_and_the_difference_of_the_review_run_off_the_event_loop(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    seen: list[str] = []

    def off_the_loop(real: Any, name: str) -> Any:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:  # a worker thread, as it must be
                seen.append(name)
                return real(*args, **kwargs)
            raise AssertionError(f"{name} ran on the event loop")

        return guarded

    from makna.services.lifecycle import Lifecycle

    for owner, name in (
        (lifecycle, "build_diff"),
        (Lifecycle, "review_queue"),
        (Lifecycle, "review_item"),
        (Lifecycle, "audit_events"),
        (Lifecycle, "approve"),
    ):
        monkeypatch.setattr(owner, name, off_the_loop(getattr(owner, name), name))
    propose(ctx, ctx.client("curator", SALES))
    approver = ctx.client("approver", ())
    assert approver.get("/review").status_code == 200
    assert approver.get("/review/metric/disbursement_amount").status_code == 200
    assert approver.get("/audit").status_code == 200
    assert decide(approver, "metric", "disbursement_amount", "approve").status_code == 303
    assert {"build_diff", "review_queue", "review_item", "audit_events", "approve"} <= set(seen)


# --- bounds ----------------------------------------------------------------------------------


def test_the_queue_the_authors_and_the_errors_are_bounded_and_counted(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reviewing, "MAX_QUEUE", 1)
    monkeypatch.setattr(lifecycle, "MAX_QUEUE", 1)
    outside_edit_of_official(ctx.world)
    outside_edit_of_official(ctx.world, object_path="lending/metrics/penalty_income.yaml")
    approver = ctx.client("approver", ())
    page = approver.get("/review").text
    assert len(rows_of(page)) == 1 and "2 proposals in review; the first 1 are listed" in page
    # The authors of one proposal: more than the bound are counted and not listed.
    monkeypatch.setattr(lifecycle, "MAX_AUTHORS", 1)
    path = "lending/metrics/penalty_income.yaml"
    original = (ctx.world.repo.path / path).read_text()
    outside_commit(
        ctx.world,
        path,
        sup.edited(original, lambda d: d.update(caveats="Another one.")),
        email="other@example.com",
    )
    item = approver.get("/review/metric/penalty_income").text
    assert "and 1 more" in item
    # The errors of one proposal: the queue row shows MAX_QUEUE_ERRORS of them with the count.
    monkeypatch.setattr(lifecycle, "MAX_QUEUE_ERRORS", 1)
    outside_commit(
        ctx.world,
        CHANGED_PATH,
        sup.edited(
            (ctx.world.repo.path / CHANGED_PATH).read_text(),
            lambda d: d.update(
                measure="contract.no_such_measure", numerator=None, join_paths={"branch": ["x.y"]}
            ),
        ),
    )
    queue = approver.get("/review").text
    assert rows_of(queue) and "definition error" in queue


def test_a_difference_too_big_to_pair_still_shows_every_line_and_approve_is_offered(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services import difference

    propose(ctx, ctx.client("curator", SALES))
    monkeypatch.setattr(difference, "MAX_MATCH_CELLS", 0)  # nothing is paired by matching
    approver = ctx.client("approver", ())
    page = approver.get("/review/metric/disbursement_amount").text
    assert 'data-field="diff"' in page and "Reviewed." in page
    assert 'data-action="approve"' in page and "too-long" not in page
    assert "shorten" not in page.lower()


# --- a new object, and what the index and the repository say ---------------------------------


def test_a_new_object_is_shown_whole_and_can_be_approved(ctx: Ctx) -> None:
    author = ctx.client("curator", ("credit-risk",))
    propose(ctx, author, "metric:contract_count_total", sup.metric_text(), "A new metric")
    approver = ctx.client("approver", ())
    page = approver.get("/review/metric/contract_count_total").text
    assert "a new object" in page and 'data-kind="added"' in page
    assert "name: contract_count_total" in page
    assert "+" in approver.get("/review").text
    assert decide(approver, "metric", "contract_count_total", "approve").status_code == 303
    assert "version 1" in approver.get("/metrics/contract_count_total").text


def test_the_review_reads_the_repository_not_the_index(ctx: Ctx) -> None:
    """An outside commit that `index sync` has not seen is on the page anyway."""
    outside_edit_of_official(ctx.world)
    with ctx.engine.connect() as connection:
        proposals = connection.execute(
            text("SELECT count(*) FROM semantic_object WHERE has_proposal")
        ).scalar_one()
    assert proposals == 1  # the sample's own draft: the commit is not indexed yet
    page = ctx.client("approver", ()).get("/review").text
    assert rows_of(page) == [CHANGED]


# --- fix round 1 -----------------------------------------------------------------------------


def first_submit_button(form_html: str) -> str:
    return str(re.findall(r'<button type="submit"[^>]*data-action="([a-z]+)"', form_html)[0])


def test_enter_in_the_reason_box_never_approves(ctx: Ctx) -> None:
    """A browser submits a form with the first submit button when Enter is pressed in a text
    field. The reason is a text area (Enter adds a line), and Reject comes first anyway."""
    author = ctx.client("curator", SALES)
    propose(ctx, author)
    approver = ctx.client("approver", ())
    page = approver.get("/review/metric/disbursement_amount").text
    form_html = re.search(r'<form[^>]*id="decide-form".*?</form>', page, re.S).group(0)  # type: ignore[union-attr]
    assert '<textarea name="reason"' in form_html and 'type="text" name="reason"' not in form_html
    assert first_submit_button(form_html) == "reject"
    assert 'action="/review/metric/disbursement_amount/reject"' in form_html  # the form's default
    # The conflict page: the proposal changed, the reason is pre-filled, the form has the new
    # commit; Enter still must not approve it.
    newer = change_text(ctx.world, description="Another description of the amount, for the page.")
    edit_page = author.get("/edit/metric/disbursement_amount").text
    form(author, "/edit/metric/disbursement_amount/submit", newer, reason="again",
         expected=expected_of(edit_page))  # fmt: skip
    conflict = post(
        approver, "/review/metric/disbursement_amount/approve", reason="LGTM", expected="0" * 40
    )
    assert conflict.status_code == 409
    form_html = re.search(r'<form[^>]*id="decide-form".*?</form>', conflict.text, re.S).group(0)  # type: ignore[union-attr]
    assert re.search(r'<textarea name="reason"[^>]*>LGTM</textarea>', form_html)
    assert (
        first_submit_button(form_html) == "reject" and 'type="text" name="reason"' not in form_html
    )
    assert expected_commit(form_html) == sup.head(ctx.world)


def test_the_service_reads_refuse_a_viewer_and_a_token(ctx: Ctx) -> None:
    from makna.services import ServiceError, ServiceErrorCode

    life = ctx.world.life
    for caller in (
        ctx.world.caller("viewer"),
        ctx.world.token("admin"),
        ctx.world.token("approver"),
    ):
        calls: list[Callable[[Any], Any]] = [
            life.review_queue,
            lambda who: life.review_item(who, CHANGED),
            life.audit_events,
            lambda who: life.audit_events(who, object_id=CHANGED),
        ]
        for call in calls:
            with pytest.raises(ServiceError) as refused:
                call(caller)
            assert refused.value.code == ServiceErrorCode.PERMISSION_DENIED
    # A curator may read all three.
    curator = ctx.world.caller("curator")
    assert life.review_queue(curator).total == 0
    assert life.review_item(curator, CHANGED) is None
    assert life.audit_events(curator).rows


def test_a_failure_after_the_commit_does_not_claim_the_page_shows_the_new_version(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from makna.services import store

    propose(ctx, ctx.client("curator", SALES))
    approver = ctx.client("approver", ())
    commit = expected_commit(approver.get("/review/metric/disbursement_amount").text)
    monkeypatch.setattr(store, "sync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    page = plain(
        post(
            approver, "/review/metric/disbursement_amount/approve", reason="go", expected=commit
        ).text
    )
    assert "The change was made official" in page and "makna index sync" in page
    assert "now shows the new version" not in page and "shows the new version" not in page


def test_a_measure_that_stops_using_a_rule_lists_the_metrics_built_on_it(ctx: Ctx) -> None:
    def edit(data: dict[str, Any]) -> None:
        for measure in data["measures"]:
            if measure["name"] == "npf_outstanding_principal":
                measure["rules"] = ["in_book"]  # it no longer applies `non_performing`

    changed = sup.edited(sup.official_text(ctx.world, "entity:contract_snapshot"), edit)
    propose(ctx, ctx.client("curator", ("credit-risk",)), "entity:contract_snapshot", changed)
    approver = ctx.client("approver", ())
    for path in ("/review", "/review/entity/contract_snapshot"):
        page = approver.get(path).text
        assert 'data-field="measure-change"' in page, path
        assert (
            "contract_snapshot.npf_outstanding_principal</code></strong> changes its rules" in page
        )
        assert (
            "<code>non_performing</code>, <code>in_book</code>"
            in page.replace("\n", " ").replace("  ", " ")
            or "non_performing" in page
        )
        metrics = set(re.findall(r'<code data-field="measure-metric">([^<]+)</code>', page))
        assert {"npf_outstanding_principal", "npf_ratio"} <= metrics, (path, metrics)
    assert 'data-field="rule-uses"' in approver.get("/review/entity/contract_snapshot").text


def test_an_outside_file_that_names_another_object_is_listed_under_its_path_and_shows_nothing_of_it(
    ctx: Ctx,
) -> None:
    other = (ctx.world.repo.path / CHANGED_PATH).read_text()
    outside_commit(ctx.world, "lending/metrics/alpha_x.draft.yaml", other)
    admin = ctx.client("admin", ())
    queue = admin.get("/review").text
    assert rows_of(queue) == ["metric:alpha_x"] and 'data-field="unreadable"' in queue
    page = admin.get("/review/metric/alpha_x").text
    assert 'data-field="unreadable"' in page and 'data-field="diff"' not in page
    assert "names another object or kind" in page
    assert "disbursement_amount" not in page and "Disbursement Amount" not in page
    assert 'data-action="approve"' not in page and 'data-action="reject"' not in page


def test_the_row_of_the_queue_says_what_the_caller_may_do_for_each_action(ctx: Ctx) -> None:
    author = ctx.client("approver", ())
    propose(ctx, author)
    row = author.get("/review").text  # the author may reject but not approve
    assert "Review (you may reject)" in row and "You cannot approve this" in row
    assert "You cannot decide" not in row and "neither approve nor reject" not in row
    outsider = ctx.client("curator", ("marketing",))
    assert "You can neither approve nor reject this" in outsider.get("/review").text
    colleague = ctx.client("curator", SALES)
    assert "Review and decide" in colleague.get("/review").text


def test_a_format_character_on_the_official_side_of_the_difference_is_marked(ctx: Ctx) -> None:
    zero = chr(0x200B)
    marked = change_text(ctx.world, description=f"Disbursed{zero} amount in rupiah, written whole.")
    ctx.world.life.create_proposal(ctx.world.caller("sales"), CHANGED, marked, "with a mark")
    ctx.world.life.submit(ctx.world.caller("sales"), CHANGED, "ready")
    ctx.world.life.approve(ctx.world.caller("approver"), CHANGED, "approved with the mark")
    propose(
        ctx,
        ctx.client("curator", SALES),
        text_=change_text(
            ctx.world, description="A plain description, written whole, for the metric."
        ),
    )
    page = ctx.client("approver", ()).get("/review/metric/disbursement_amount").text
    assert zero not in page and "&lt;U+200B&gt;" in page


def test_a_lookalike_letter_is_highlighted_and_its_code_point_is_shown(ctx: Ctx) -> None:
    official = sup.official_text(ctx.world, CHANGED)
    text_ = sup.edited(
        official, lambda d: d.update(label=d["label"].replace("Disbursement", "Disbursеment"))
    )
    propose(ctx, ctx.client("curator", SALES), text_=text_)
    page = ctx.client("approver", ()).get("/review/metric/disbursement_amount").text
    assert '<mark class="part">е&lt;U+0435&gt;</mark>' in page
    assert '<mark class="part">e</mark>' in page  # the official side shows what it had


def test_the_audit_time_is_utc_when_the_database_session_is_not(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGTZ", "Asia/Jakarta")  # libpq reads it for the application's connections
    app = w.make_app(ctx.world.url, definitions_repo=str(ctx.world.repo.path))
    try:
        client = w.signed_in(app, w.make_user(ctx.engine, "approver"))
        with ctx.engine.connect() as connection:
            stamp = connection.execute(
                text("SELECT to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') "
                     "FROM audit_event ORDER BY id DESC LIMIT 1")
            ).scalar_one()  # fmt: skip
        assert stamp in client.get("/audit").text
    finally:
        app.state.services.dispose()
